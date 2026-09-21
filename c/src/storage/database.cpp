#include "orders2/database.h"
#include <limits>
#include <utility>

namespace orders2 {
namespace {
void check(sqlite3* db, int rc) {
    if (rc != SQLITE_OK) throw DatabaseError(rc, sqlite3_errmsg(db));
}
int length(std::size_t n) {
    if (n > static_cast<std::size_t>(std::numeric_limits<int>::max()))
        throw std::length_error("SQLite input too large");
    return static_cast<int>(n);
}
}
DatabaseError::DatabaseError(int code, std::string message)
    : std::runtime_error(std::move(message)), code_(code) {}
Statement::Statement(sqlite3* db, std::string_view sql) : db_(db) {
    check(db_, sqlite3_prepare_v2(db_, sql.data(), length(sql.size()), &statement_, nullptr));
}
Statement::~Statement() { sqlite3_finalize(statement_); }
void Statement::bind(int i, std::string_view v) {
    check(db_, sqlite3_bind_text(statement_, i, v.empty() ? "" : v.data(), length(v.size()), SQLITE_TRANSIENT));
}
void Statement::bind(int i, std::int64_t v) { check(db_, sqlite3_bind_int64(statement_, i, v)); }
bool Statement::row() {
    const int rc = sqlite3_step(statement_);
    if (rc == SQLITE_ROW) return true;
    if (rc == SQLITE_DONE) return false;
    check(db_, rc);
    return false;
}
void Statement::execute() {
    if (row()) throw DatabaseError(SQLITE_MISUSE, "unexpected rows in mutation");
}
std::string Statement::text(int c) const {
    const auto* value = sqlite3_column_text(statement_, c);
    return value ? std::string(reinterpret_cast<const char*>(value),
                              static_cast<std::size_t>(sqlite3_column_bytes(statement_, c))) : std::string{};
}
std::int64_t Statement::integer(int c) const { return sqlite3_column_int64(statement_, c); }
Database::Database(const std::string& path) {
    const int rc = sqlite3_open_v2(path.c_str(), &db_, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX, nullptr);
    if (rc != SQLITE_OK) {
        const std::string error = db_ ? sqlite3_errmsg(db_) : "cannot allocate SQLite connection";
        sqlite3_close(db_);
        db_ = nullptr;
        throw DatabaseError(rc, error);
    }
    try {
        check(db_, sqlite3_extended_result_codes(db_, 1));
        check(db_, sqlite3_busy_timeout(db_, 5000));
        exec("PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;");
    } catch (...) { sqlite3_close(db_); db_ = nullptr; throw; }
}
Database::~Database() { sqlite3_close(db_); }
void Database::exec(std::string_view sql) {
    check(db_, sqlite3_exec(db_, std::string(sql).c_str(), nullptr, nullptr, nullptr));
}
int Database::changes() const { return sqlite3_changes(db_); }
Transaction::Transaction(Database& db) : db_(db) { db_.exec("BEGIN IMMEDIATE"); }
Transaction::~Transaction() {
    if (!committed_) { try { db_.exec("ROLLBACK"); } catch (...) {} }
}
void Transaction::commit() { db_.exec("COMMIT"); committed_ = true; }
} // namespace orders2
