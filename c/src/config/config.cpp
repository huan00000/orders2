#include "orders2/config.h"
#include "orders2/domain.h"
#include "orders2/store.h"
#include <cerrno>
#include <charconv>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iterator>
#include <limits>
#include <sstream>
#include <system_error>
#include <utility>
#include <vector>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

namespace orders2 {
namespace {
std::string trim(std::string_view s) {
    const auto first=s.find_first_not_of(" \t\r\n");
    if (first==std::string_view::npos) return {};
    return std::string(s.substr(first,s.find_last_not_of(" \t\r\n")-first+1));
}
std::string read(const std::filesystem::path& path) {
    std::ifstream in(path,std::ios::binary);
    if (!in) throw RuleError("cannot read configuration file");
    std::string data{std::istreambuf_iterator<char>(in),std::istreambuf_iterator<char>()};
    if (in.bad()) throw RuleError("configuration read failed");
    return data;
}
int integer(const ConfigValues& v, const std::string& key, int fallback, int minimum) {
    const auto it=v.find(key); if (it==v.end()) return fallback;
    int result{};
    const auto& s=it->second;
    const auto parsed=std::from_chars(s.data(),s.data()+s.size(),result);
    if (parsed.ec!=std::errc{} || parsed.ptr!=s.data()+s.size() || result<minimum)
        throw RuleError("invalid integer setting: " + key);
    return result;
}
double positive(const ConfigValues& v, const std::string& key, double fallback) {
    const auto it=v.find(key); if (it==v.end()) return fallback;
    std::size_t end{}; double result{};
    try { result=std::stod(it->second,&end); }
    catch (...) { throw RuleError("invalid numeric setting: " + key); }
    if (end!=it->second.size() || !std::isfinite(result) || result<=0)
        throw RuleError("setting must be finite and positive: " + key);
    return result;
}
[[noreturn]] void io_error(const char* operation) {
    throw std::system_error(errno,std::generic_category(),operation);
}
class Temporary {
public:
    explicit Temporary(const std::filesystem::path& destination) {
        auto pattern=destination.string()+".tmp.XXXXXX";
        std::vector<char> buffer(pattern.begin(),pattern.end()); buffer.push_back('\0');
        fd_=::mkstemp(buffer.data());
        if (fd_<0) io_error("create configuration temporary file");
        path_=buffer.data();
    }
    ~Temporary() {
        if (fd_>=0) ::close(fd_);
        if (!path_.empty()) ::unlink(path_.c_str());
    }
    Temporary(const Temporary&)=delete;
    Temporary& operator=(const Temporary&)=delete;
    void write(std::string_view data) {
        while (!data.empty()) {
            const auto amount=::write(fd_,data.data(),data.size());
            if (amount<0) { if (errno==EINTR) continue; io_error("write configuration"); }
            if (amount==0) throw RuleError("zero-length configuration write");
            data.remove_prefix(static_cast<std::size_t>(amount));
        }
        if (::fsync(fd_)!=0) io_error("sync configuration");
    }
    void replace(const std::filesystem::path& destination) {
        if (::rename(path_.c_str(),destination.c_str())!=0) io_error("replace configuration");
        path_.clear();
    }
private:
    int fd_{-1};
    std::string path_;
};
class DirectorySync {
public:
    explicit DirectorySync(const std::filesystem::path& path) {
        fd_=::open(path.c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC);
        if (fd_<0) io_error("open configuration directory");
    }
    ~DirectorySync() { ::close(fd_); }
    void sync() { if (::fsync(fd_)!=0) io_error("sync configuration directory"); }
private:
    int fd_{};
};
}
ConfigValues parse_env(std::string_view text, bool strict) {
    if (text.find('\0')!=std::string_view::npos) throw RuleError("configuration contains NUL");
    ConfigValues values;
    std::istringstream lines{std::string(text)};
    std::string line;
    while (std::getline(lines,line)) {
        line=trim(line);
        if (line.empty() || line.front()=='#') continue;
        const auto equal=line.find('=');
        if (equal==std::string::npos) {
            if (strict) throw RuleError("configuration line must contain '='");
            continue; // old loader ignored these lines
        }
        const auto key=trim(std::string_view(line).substr(0,equal));
        auto value=trim(std::string_view(line).substr(equal+1));
        if (key.empty() || key.find_first_not_of("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")!=std::string::npos)
            throw RuleError("invalid configuration key");
        if (strict && values.contains(key)) throw RuleError("duplicate configuration key: " + key);
        if (strict && !value.empty() && (value.front()=='\'' || value.front()=='"') &&
            (value.size()<2 || value.back()!=value.front())) throw RuleError("unclosed configuration quote");
        // Match general.py strip('"\''); no interpolation, inline comment or escape expansion.
        const auto first=value.find_first_not_of("\"'");
        value=first==std::string::npos ? "" : value.substr(first,value.find_last_not_of("\"'")-first+1);
        values[key]=value;
    }
    return values;
}
bool Config::has_credentials() const {
    const auto key=values.find("API_KEY"), secret=values.find("API_SECRET");
    return key!=values.end() && secret!=values.end() && !key->second.empty() && !secret->second.empty();
}
Config make_config(ConfigValues values, bool require_credentials) {
    Config c; c.values=std::move(values);
    if (require_credentials && !c.has_credentials()) throw RuleError("API_KEY and API_SECRET are required");
    c.poll_seconds=positive(c.values,"POLL_INTERVAL_SECONDS",60);
    c.request_timeout_seconds=positive(c.values,"REQUEST_TIMEOUT_SECONDS",20);
    c.log_retention_days=integer(c.values,"LOG_RETENTION_DAYS",30,0);
    c.trade_retention_days=integer(c.values,"TRADE_RETENTION_DAYS",0,0);
    c.intent_retention_days=integer(c.values,"INTENT_RETENTION_DAYS",0,0);
    c.state_retention_days=integer(c.values,"STATE_RETENTION_DAYS",0,0);
    c.manual_retention_days=integer(c.values,"MANUAL_RETENTION_DAYS",0,0);
    c.backup_interval_seconds=integer(c.values,"BACKUP_INTERVAL_SECONDS",3600,1);
    c.scheduled_backup_count=integer(c.values,"SCHEDULED_BACKUP_COUNT",24,1);
    c.upgrade_backup_count=integer(c.values,"UPGRADE_BACKUP_COUNT",5,1);
    return c;
}
Config load_config(const std::filesystem::path& file, Environment env) {
    ConfigValues values;
    if (std::filesystem::exists(file)) values=parse_env(read(file));
    if (!env) env=[](const std::string& key)->std::optional<std::string> {
        const char* value=std::getenv(key.c_str());
        return value ? std::optional<std::string>(value) : std::nullopt;
    };
    for (const auto* key : {"API_KEY","API_SECRET","GHCR_IMAGE","POLL_INTERVAL_SECONDS","REQUEST_TIMEOUT_SECONDS",
         "LOG_RETENTION_DAYS","TRADE_RETENTION_DAYS","INTENT_RETENTION_DAYS","STATE_RETENTION_DAYS",
         "MANUAL_RETENTION_DAYS","BACKUP_INTERVAL_SECONDS","SCHEDULED_BACKUP_COUNT","UPGRADE_BACKUP_COUNT"})
        if (auto value=env(key)) values[key]=*value;
    // Preserve environment precedence for legacy/custom keys already in the file.
    for (auto& [key,value] : values) if (auto override_value=env(key)) value=*override_value;
    return make_config(std::move(values),false); // Empty first deployment is permitted.
}
void save_config(Store& store, const std::filesystem::path& file, std::string_view text) {
    (void)make_config(parse_env(text,true),true);
    store.with_all_tasks_stopped([&] {
        // Mount the directory: replacing a directly bind-mounted file is not portable.
        if (std::filesystem::is_symlink(std::filesystem::symlink_status(file)))
            throw RuleError("configuration destination must not be a symlink");
        const auto directory=file.has_parent_path() ? file.parent_path() : std::filesystem::path(".");
        DirectorySync sync(directory);
        Temporary next(file); next.write(text);
        if (std::filesystem::exists(file)) {
            if (!std::filesystem::is_regular_file(file)) throw RuleError("configuration is not a regular file");
            const auto previous=read(file);
            const std::filesystem::path backup=file.string()+".bak";
            Temporary backup_file(backup); backup_file.write(previous); backup_file.replace(backup);
            sync.sync();
        }
        next.replace(file);
        sync.sync();
    });
}
} // namespace orders2
