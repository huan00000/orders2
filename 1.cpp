// C++17; dependencies: libcurl, OpenSSL (link: -lcurl -lcrypto).
// Compile with an absolute source path so __FILE__ locates this file's .env.
// No main(): loading configuration never sends a request.
// Usage: auto config = load_config(); auto response = get_positions(config);
// Each request returns the response body, or throws std::runtime_error on failure.
// .env keys (API_KEY/API_SECRET required for authenticated requests):
// API_KEY=...                 API_SECRET=...
// CONTRACT=BTC_USDT           LEVERAGE=10          MARGIN_MODE=cross
// AMOUNT=10                   ACTIVATION_PRICE=50000
// IS_GTE=true                PRICE_TYPE=1         PRICE_OFFSET=0.1%
// REDUCE_ONLY=false          TEXT=apiv4           ORDER_ID=123456789
// Put each key=value on its own line. Unquoted or single/double quoted values
// are supported; quotes are literal (no variable expansion or escape decoding).

#include <curl/curl.h>
#include <openssl/evp.h>
#include <openssl/hmac.h>

#include <chrono>
#include <climits>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>

struct GateConfig {
    std::string api_key, api_secret;
    std::string contract = "BTC_USDT";
    std::string leverage = "10", margin_mode = "cross";
    std::string amount = "10", activation_price = "50000";
    bool is_gte = true;
    int price_type = 1;
    std::string price_offset = "0.1%";
    bool reduce_only = false;
    std::string text = "apiv4";
    // Preserve the two different IDs in the original examples unless overridden.
    std::uint64_t stop_order_id = 123456789, detail_order_id = 0;
};

namespace {
// 去掉字符串开头和结尾的空格、制表符和换行；如果全是空白，就返回空字符串。
std::string trim(const std::string& value) {
    const auto first = value.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) return {};
    return value.substr(first, value.find_last_not_of(" \t\r\n") - first + 1);
}

// 先检查内容是不是非空的纯数字，再转成无符号整数；格式不对或数字太大就报错。
std::uint64_t unsigned_number(const std::string& value, const std::string& key) {
    if (value.empty() || value.find_first_not_of("0123456789") != std::string::npos)
        throw std::runtime_error("Invalid unsigned integer: " + key);
    try {
        return std::stoull(value);
    } catch (const std::exception&) {
        throw std::runtime_error("Integer out of range: " + key);
    }
}

// 挨个读取字节，把每个字节拆成两个小写十六进制字符，最后拼成字符串返回。
std::string hex(const unsigned char* bytes, unsigned int count) {
    const char* digits = "0123456789abcdef";
    std::string result;
    result.reserve(count * 2);
    for (unsigned int i = 0; i < count; ++i) {
        result += digits[bytes[i] >> 4];
        result += digits[bytes[i] & 15];
    }
    return result;
}

// 先给请求正文算 SHA512 摘要，再按顺序用换行拼上方法、路径、查询参数、摘要和时间戳。
// 检查密钥长度后，用 API 密钥对拼好的内容做 HMAC-SHA512 签名，转成十六进制返回；计算失败就报错。
std::string sign_request(const GateConfig& config, const std::string& method,
                         const std::string& path, const std::string& query,
                         const std::string& body, const std::string& timestamp) {
    unsigned char digest[EVP_MAX_MD_SIZE];
    unsigned int length = 0;
    if (EVP_Digest(body.data(), body.size(), digest, &length, EVP_sha512(), nullptr) != 1)
        throw std::runtime_error("SHA512 failed");
    const std::string message = method + "\n" + path + "\n" + query + "\n" +
                                hex(digest, length) + "\n" + timestamp;
    if (config.api_secret.size() > INT_MAX)
        throw std::runtime_error("API_SECRET is too long");
    if (!HMAC(EVP_sha512(), config.api_secret.data(),
              static_cast<int>(config.api_secret.size()),
              reinterpret_cast<const unsigned char*>(message.data()), message.size(),
              digest, &length))
        throw std::runtime_error("HMAC-SHA512 failed");
    return hex(digest, length);
}

// 逐个检查字节：字母、数字和 -_.~ 原样保留，其他字节转成 % 加两位十六进制，供网址使用。
std::string url_encode(const std::string& value) {
    const char* digits = "0123456789ABCDEF";
    std::string result;
    for (unsigned char c : value) {
        if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
            (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' || c == '~') {
            result += static_cast<char>(c);
        } else {
            result += '%'; result += digits[c >> 4]; result += digits[c & 15];
        }
    }
    return result;
}

// 给字符串套上双引号，并转义里面的引号、反斜杠和控制字符，返回能放进 JSON 的字符串。
std::string json_string(const std::string& value) {
    const char* digits = "0123456789abcdef";
    std::string result = "\"";
    for (unsigned char c : value) {
        if (c == '"' || c == '\\') {
            result += '\\'; result += static_cast<char>(c);
        } else if (c < 32) {
            result += "\\u00"; result += digits[c >> 4]; result += digits[c & 15];
        } else {
            result += static_cast<char>(c);
        }
    }
    return result + '"';
}

struct CurlRuntime {
    // 创建这个对象时先初始化 libcurl 的全局环境；初始化失败就报错。
    CurlRuntime() {
        if (curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK)
            throw std::runtime_error("curl_global_init failed");
    }
    // 对象销毁时清理 libcurl 的全局环境，释放它占用的资源。
    ~CurlRuntime() { curl_global_cleanup(); }
};

// libcurl 每收到一块响应数据就调用这里：先检查长度会不会溢出，再把数据追加到输出字符串。
// 成功就返回接收的字节数；溢出或追加失败就返回 0，让 libcurl 中止，异常不会传进 C 库。
std::size_t receive_body(char* data, std::size_t size, std::size_t count, void* output) noexcept {
    if (size != 0 && count > SIZE_MAX / size) return 0;
    const auto length = size * count;
    try {
        static_cast<std::string*>(output)->append(data, length);
        return length;
    } catch (...) {
        return 0; // Never propagate a C++ exception through libcurl.
    }
}

// 把一项请求设置交给 libcurl；设置失败就立即报错，避免带着不完整的设置继续请求。
template <typename T>
void set_option(CURL* curl, CURLoption option, T value) {
    if (curl_easy_setopt(curl, option, value) != CURLE_OK)
        throw std::runtime_error("curl_easy_setopt failed");
}

// 先准备 libcurl、请求地址和请求头；需要身份验证时，检查密钥并加上时间戳和签名。
// 再设置请求方法、超时、响应接收方式，以及 POST 正文，然后真正发送请求。
// 网络出错或 HTTP 状态码不在 200～299 就报错；成功则原样返回响应正文，相关资源自动释放。
std::string request(const GateConfig& config, const std::string& method,
                    const std::string& endpoint, const std::string& query = {},
                    const std::string& body = {}, bool authenticated = true) {
    static const CurlRuntime runtime;
    std::unique_ptr<CURL, decltype(&curl_easy_cleanup)> curl(curl_easy_init(), curl_easy_cleanup);
    if (!curl) throw std::runtime_error("curl_easy_init failed");
    std::unique_ptr<curl_slist, decltype(&curl_slist_free_all)> headers(nullptr, curl_slist_free_all);
    // 往请求头列表追加一项，成功后接管更新后的列表；追加失败就报错。
    auto add_header = [&](const std::string& value) {
        auto* updated = curl_slist_append(headers.get(), value.c_str());
        if (!updated) throw std::runtime_error("curl_slist_append failed");
        headers.release();
        headers.reset(updated);
    };
    const std::string path = "/api/v4" + endpoint;
    const std::string url = "https://api.gateio.ws" + path + (query.empty() ? "" : "?" + query);
    add_header("Accept: application/json");
    if (!body.empty()) add_header("Content-Type: application/json");
    if (authenticated) {
        if (config.api_key.empty() || config.api_secret.empty())
            throw std::runtime_error("API_KEY and API_SECRET are required");
        if (config.api_key.find_first_of("\r\n") != std::string::npos ||
            config.api_key.find('\0') != std::string::npos)
            throw std::runtime_error("Invalid API_KEY");
        const auto seconds = std::chrono::duration_cast<std::chrono::seconds>(
            std::chrono::system_clock::now().time_since_epoch()).count();
        const std::string timestamp = std::to_string(seconds);
        add_header("Timestamp: " + timestamp);
        add_header("KEY: " + config.api_key);
        add_header("SIGN: " + sign_request(config, method, path, query, body, timestamp));
    }
    std::string response;
    set_option(curl.get(), CURLOPT_URL, url.c_str());
    set_option(curl.get(), CURLOPT_CUSTOMREQUEST, method.c_str());
    set_option(curl.get(), CURLOPT_HTTPHEADER, headers.get());
    set_option(curl.get(), CURLOPT_CONNECTTIMEOUT, 10L);
    set_option(curl.get(), CURLOPT_TIMEOUT, 30L);
    set_option(curl.get(), CURLOPT_NOSIGNAL, 1L);
    set_option(curl.get(), CURLOPT_WRITEFUNCTION, &receive_body);
    set_option(curl.get(), CURLOPT_WRITEDATA, &response);
    if (method == "POST") {
        set_option(curl.get(), CURLOPT_POSTFIELDS, body.c_str());
        set_option(curl.get(), CURLOPT_POSTFIELDSIZE_LARGE, static_cast<curl_off_t>(body.size()));
    }
    const auto result = curl_easy_perform(curl.get());
    if (result != CURLE_OK)
        throw std::runtime_error(std::string("HTTP transport failed: ") + curl_easy_strerror(result));
    long status = 0;
    if (curl_easy_getinfo(curl.get(), CURLINFO_RESPONSE_CODE, &status) != CURLE_OK)
        throw std::runtime_error("Cannot read HTTP status");
    if (status < 200 || status >= 300)
        throw std::runtime_error("HTTP " + std::to_string(status) + ": " + response);
    return response;
}
} // namespace

// 打开指定的 .env（默认在源码同目录），逐行去掉首尾空白，跳过空行和注释，拆出键和值。
// 同时处理首行 BOM、export 前缀和包住值的引号；重复的键以后面的为准，格式或读取出错就报错。
// 把读到的值填进配置，没写的保留默认值；检查布尔值、价格类型和订单 ID 后返回配置，不发送请求。
GateConfig load_config(const std::filesystem::path& env_path =
                          std::filesystem::path(__FILE__).parent_path() / ".env") {
    std::ifstream input(env_path, std::ios::binary);
    if (!input) throw std::runtime_error("Cannot open .env: " + env_path.string());
    std::map<std::string, std::string> values;
    std::string line;
    std::size_t line_number = 0;
    while (std::getline(input, line)) {
        ++line_number;
        if (line_number == 1 && line.compare(0, 3, "\xEF\xBB\xBF") == 0) line.erase(0, 3);
        line = trim(line);
        if (line.empty() || line.front() == '#') continue;
        if (line.compare(0, 7, "export ") == 0) line = trim(line.substr(7));
        const auto equal = line.find('=');
        if (equal == std::string::npos || trim(line.substr(0, equal)).empty())
            throw std::runtime_error("Invalid .env entry on line " + std::to_string(line_number));
        const auto key = trim(line.substr(0, equal));
        auto value = trim(line.substr(equal + 1));
        if (!value.empty() && (value.front() == '\'' || value.front() == '"')) {
            if (value.size() < 2 || value.back() != value.front())
                throw std::runtime_error("Unclosed .env quote on line " + std::to_string(line_number));
            value = value.substr(1, value.size() - 2);
        }
        values[key] = value;
    }
    if (input.bad()) throw std::runtime_error("Cannot read .env");
    GateConfig config;
    // 按名字查找一项文字配置：找到了就覆盖目标值，没找到就保留原来的默认值。
    auto read = [&](const char* key, std::string& target) {
        const auto entry = values.find(key);
        if (entry != values.end()) target = entry->second;
    };
    read("API_KEY", config.api_key); read("API_SECRET", config.api_secret);
    read("CONTRACT", config.contract); read("LEVERAGE", config.leverage);
    read("MARGIN_MODE", config.margin_mode); read("AMOUNT", config.amount);
    read("ACTIVATION_PRICE", config.activation_price); read("PRICE_OFFSET", config.price_offset);
    read("TEXT", config.text);
    // 按名字读取布尔配置：没写就保留默认值，true/1 当作真，false/0 当作假，其他写法报错。
    auto read_bool = [&](const char* key, bool& target) {
        const auto entry = values.find(key);
        if (entry == values.end()) return;
        if (entry->second == "true" || entry->second == "1") target = true;
        else if (entry->second == "false" || entry->second == "0") target = false;
        else throw std::runtime_error(std::string("Invalid boolean: ") + key);
    };
    read_bool("IS_GTE", config.is_gte); read_bool("REDUCE_ONLY", config.reduce_only);
    if (values.count("PRICE_TYPE")) {
        const auto type = unsigned_number(values.at("PRICE_TYPE"), "PRICE_TYPE");
        if (type > INT_MAX) throw std::runtime_error("PRICE_TYPE out of range");
        config.price_type = static_cast<int>(type);
    }
    if (values.count("ORDER_ID")) {
        config.stop_order_id = config.detail_order_id = unsigned_number(values.at("ORDER_ID"), "ORDER_ID");
    }
    return config;
}

// 1. Query contract (public endpoint).
// 先把合约名编码后放进网址，再发起不需要签名的 GET 请求，返回合约信息的原始正文。
std::string get_contract(const GateConfig& config) {
    return request(config, "GET", "/futures/usdt/contracts/" + url_encode(config.contract), {}, {}, false);
}

// 2. Query positions.
// 调用统一请求函数，签名后用 GET 查询 USDT 合约持仓，返回接口的原始正文。
std::string get_positions(const GateConfig& config) {
    return request(config, "GET", "/futures/usdt/positions");
}

// 3. Set leverage; retain the original endpoint and query parameters.
// 把合约名、杠杆倍数和保证金模式编码，分别放进路径和查询参数，再签名发送 POST，返回结果正文。
std::string set_leverage(const GateConfig& config) {
    return request(config, "POST", "/futures/usdt/positions/" + url_encode(config.contract) + "/set_leverage",
                   "leverage=" + url_encode(config.leverage) + "&margin_mode=" + url_encode(config.margin_mode));
}

// 4. Create trailing order.
// 从配置取出合约、数量、激活价、触发方向、价格类型、回调幅度、只减仓标记和备注，拼成 JSON。
// 文字做 JSON 转义，布尔值和价格类型直接写入；再签名发送 POST 创建追踪订单，返回结果正文。
std::string create_trailing_order(const GateConfig& config) {
    const std::string body = "{\"contract\":" + json_string(config.contract) +
        ",\"amount\":" + json_string(config.amount) +
        ",\"activation_price\":" + json_string(config.activation_price) +
        ",\"is_gte\":" + (config.is_gte ? "true" : "false") +
        ",\"price_type\":" + std::to_string(config.price_type) +
        ",\"price_offset\":" + json_string(config.price_offset) +
        ",\"reduce_only\":" + (config.reduce_only ? "true" : "false") +
        ",\"text\":" + json_string(config.text) + "}";
    return request(config, "POST", "/futures/usdt/autoorder/v1/trail/create", {}, body);
}

// 5. Stop trailing order.
// 把要停止的追踪订单 ID 拼成 JSON，签名后发送 POST 停止订单，返回结果正文。
std::string stop_trailing_order(const GateConfig& config) {
    return request(config, "POST", "/futures/usdt/autoorder/v1/trail/stop", {},
                   "{\"id\":" + std::to_string(config.stop_order_id) + "}");
}

// 6. Query trailing order details.
// 把要查询的追踪订单 ID 放进查询参数，签名后发送 GET，返回订单详情的原始正文。
std::string get_trailing_order_detail(const GateConfig& config) {
    return request(config, "GET", "/futures/usdt/autoorder/v1/trail/detail",
                   "id=" + std::to_string(config.detail_order_id));
}
