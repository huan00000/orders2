#pragma once
#include <sqlite3.h>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>

namespace orders2 {
class DatabaseError : public std::runtime_error {
public:
    DatabaseError(int code, std::string message);
    int code() const noexcept { return code_; }
    bool constraint() const noexcept { return (code_ & 0xff) == SQLITE_CONSTRAINT; }
private:
    int code_;
};
class Statement {
public:
    Statement(sqlite3* db, std::string_view sql);
    ~Statement();
    Statement(const Statement&) = delete;
    Statement& operator=(const Statement&) = delete;
    void bind(int index, std::string_view value);
    void bind(int index, std::int64_t value);
    bool row();
    void execute();
    std::string text(int column) const;
    std::int64_t integer(int column) const;
private:
    sqlite3* db_;
    sqlite3_stmt* statement_{};
};
class Database {
public:
    explicit Database(const std::string& path);
    ~Database();
    Database(const Database&) = delete;
    Database& operator=(const Database&) = delete;
    void exec(std::string_view sql);
    Statement prepare(std::string_view sql) { return Statement(db_, sql); }
    int changes() const;
private:
    sqlite3* db_{};
};
class Transaction {
public:
    explicit Transaction(Database& db);
    ~Transaction();
    Transaction(const Transaction&) = delete;
    Transaction& operator=(const Transaction&) = delete;
    void commit();
private:
    Database& db_;
    bool committed_{false};
};
} // namespace orders2
