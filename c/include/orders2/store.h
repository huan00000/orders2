#pragma once
#include "orders2/database.h"
#include "orders2/domain.h"
#include <functional>
#include <mutex>
#include <optional>
#include <set>
#include <vector>

namespace orders2 {
// One daemon owns a Store. TUI never opens a writing connection. Callback results
// and full_intent must be sanitized by the exchange adapter before persistence.
class Store {
public:
    explicit Store(const std::string& path);
    void create_task(const Task& task, Timestamp now);
    void create_goal(const std::string& id, const std::string& task_id, Timestamp now);
    DispatchResult dispatch(const Operation& operation, const std::string& full_intent,
                            Timestamp now, const std::function<OperationResult()>& send,
                            const std::function<Timestamp()>& result_clock = {});
    // Unknown operations are resolved only from affirmative exchange evidence.
    // A missing order/search miss is not evidence and must remain Unknown.
    void resolve(const std::string& operation_id, const OperationResult& result,
                 const std::string& full_intent, const std::string& evidence, Timestamp now);
    std::optional<OperationRecord> operation(const std::string& id);
    std::vector<OperationRecord> unresolved();
    void apply_order_event(const std::string& id, OrderEvent event, EventSource source,
                           const std::string& reason, Timestamp now);
    OrderState order_status(const std::string& id);
    bool account_blocked(const std::string& account) const;
    // Holds both the process mutex and a SQLite write transaction over file save.
    void with_all_tasks_stopped(const std::function<void()>& save);
private:
    Database db_;
    mutable std::recursive_mutex mutex_;
    std::set<std::string> blocked_accounts_;
    void initialize();
    void require_writable(const std::string& account) const;
    void write(const std::string& account, const std::function<void()>& action,
               bool constraint_is_conflict = false);
    std::string goal_account(const std::string& goal_id);
    void finish(const std::string& id, const OperationResult& result,
                const std::string& full_intent, Timestamp now);
    void order_event(const std::string& id, OrderEvent event, EventSource source,
                     const std::string& reason, Timestamp now);
};
} // namespace orders2
