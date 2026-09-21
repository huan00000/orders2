#pragma once
#include <filesystem>
#include <functional>
#include <map>
#include <optional>
#include <string>
#include <string_view>

namespace orders2 {
class Store;
using ConfigValues = std::map<std::string, std::string>;
using Environment = std::function<std::optional<std::string>(const std::string&)>;
struct Config {
    ConfigValues values; // Credentials stay here/in the mounted file, never SQLite.
    double poll_seconds{60}, request_timeout_seconds{20};
    int log_retention_days{30}, trade_retention_days{0}; // zero = permanent
    int intent_retention_days{0}, state_retention_days{0}, manual_retention_days{0};
    int backup_interval_seconds{3600}, scheduled_backup_count{24}, upgrade_backup_count{5};
    bool has_credentials() const;
};
ConfigValues parse_env(std::string_view text, bool strict = false);
Config make_config(ConfigValues values, bool require_credentials);
Config load_config(const std::filesystem::path& file, Environment environment = {});
// Validation uses the proposed file alone, not environment overrides. Stores the
// original text (including comments), preserving the legacy key=value format.
void save_config(Store& store, const std::filesystem::path& file, std::string_view text);
} // namespace orders2
