#include "orders2/store.h"
#include <chrono>

namespace orders2 {
namespace {
void required(const std::string& value) {
    if (value.empty()) throw RuleError("required identifier is empty");
}
OperationRecord record(Statement& q) {
    return {q.text(0), q.text(1), q.text(2), q.text(3), q.text(4)};
}
constexpr auto operation_columns = "SELECT id,goal_id,phase,exchange_order_id,result FROM operations ";
}
Store::Store(const std::string& path) : db_(path) { initialize(); }
void Store::initialize() {
    Transaction tx(db_);
    std::int64_t v{};
    {
        auto version = db_.prepare("PRAGMA user_version");
        if (!version.row()) throw RuleError("missing schema version");
        v = version.integer(0);
    }
    if (v != 0 && v != 1) throw RuleError("unsupported database schema; no automatic downgrade");
    if (v == 0) db_.exec(R"sql(
CREATE TABLE tasks(
 id TEXT PRIMARY KEY NOT NULL CHECK(length(id)>0), account TEXT NOT NULL CHECK(length(account)>0),
 contract TEXT NOT NULL CHECK(length(contract)>0), direction TEXT NOT NULL CHECK(direction IN ('long','short')),
 state TEXT NOT NULL CHECK(state IN ('stopped','running','paused','pause_pending','stop_pending',
 'reconciling','recovery_delay','awaiting_manual','storage_blocked')),
 desired_state TEXT NOT NULL CHECK(desired_state IN ('stopped','running','paused')),
 updated_at INTEGER NOT NULL);
CREATE UNIQUE INDEX one_active_task ON tasks(account,contract,direction) WHERE state <> 'stopped';
CREATE TABLE goals(
 id TEXT PRIMARY KEY NOT NULL CHECK(length(id)>0), task_id TEXT NOT NULL REFERENCES tasks(id),
 state TEXT NOT NULL CHECK(state IN ('active','paused','completed','stopped')),
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE UNIQUE INDEX one_unfinished_goal ON goals(task_id) WHERE state IN ('active','paused');
CREATE TABLE operations(
 id TEXT PRIMARY KEY NOT NULL CHECK(length(id)>0), goal_id TEXT NOT NULL REFERENCES goals(id),
 kind TEXT NOT NULL CHECK(kind IN ('open','cancel','close')),
 request_digest TEXT NOT NULL CHECK(length(request_digest)>0), client_tag TEXT NOT NULL,
 target_order_id TEXT NOT NULL,
 phase TEXT NOT NULL CHECK(phase IN ('prepared','sending','unknown','succeeded','failed')),
 exchange_order_id TEXT NOT NULL DEFAULT '', full_intent TEXT NOT NULL DEFAULT '',
 result TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 CHECK((kind='cancel' AND length(target_order_id)>0) OR (kind<>'cancel' AND length(client_tag)>0)));
CREATE UNIQUE INDEX one_unfinished_operation ON operations(goal_id) WHERE phase IN ('prepared','sending','unknown');
CREATE UNIQUE INDEX unique_client_tag ON operations(client_tag) WHERE client_tag<>'';
CREATE TABLE orders(
 id TEXT PRIMARY KEY NOT NULL CHECK(length(id)>0), operation_id TEXT NOT NULL UNIQUE REFERENCES operations(id),
 state TEXT NOT NULL CHECK(state IN ('pending_submit','submitting','submitted','partially_filled','filled',
 'cancel_pending','cancelled','rejected','unknown','reconciling')), updated_at INTEGER NOT NULL);
CREATE TABLE state_history(
 id INTEGER PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
 from_state TEXT NOT NULL, to_state TEXT NOT NULL, at INTEGER NOT NULL,
 reason TEXT NOT NULL CHECK(length(reason)>0), source TEXT NOT NULL);
CREATE TABLE fills(
 account TEXT NOT NULL, exchange_fill_id TEXT NOT NULL, order_id TEXT NOT NULL REFERENCES orders(id),
 quantity TEXT NOT NULL, price TEXT NOT NULL, at INTEGER NOT NULL, PRIMARY KEY(account,exchange_fill_id));
CREATE TABLE positions(
 account TEXT NOT NULL, contract TEXT NOT NULL, direction TEXT NOT NULL CHECK(direction IN ('long','short')),
 quantity TEXT NOT NULL, entry_price TEXT NOT NULL, observed_at INTEGER NOT NULL,
 PRIMARY KEY(account,contract,direction));
CREATE TABLE manual_actions(
 id TEXT PRIMARY KEY NOT NULL, task_id TEXT REFERENCES tasks(id), action TEXT NOT NULL,
 detail TEXT NOT NULL, actor TEXT NOT NULL, at INTEGER NOT NULL);
CREATE TABLE logs(id INTEGER PRIMARY KEY, task_id TEXT REFERENCES tasks(id),
 level TEXT NOT NULL, message TEXT NOT NULL, at INTEGER NOT NULL);
CREATE INDEX log_time ON logs(at);
CREATE TABLE runtime_metadata(key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL);
PRAGMA user_version=1;
)sql");
    tx.commit();
}
bool Store::account_blocked(const std::string& account) const {
    std::lock_guard lock(mutex_);
    return blocked_accounts_.contains(account);
}
void Store::require_writable(const std::string& account) const {
    if (blocked_accounts_.contains(account)) throw RuleError("account storage fault; manual recovery required");
}
void Store::write(const std::string& account, const std::function<void()>& action, bool constraint_is_conflict) {
    require_writable(account);
    try {
        Transaction tx(db_);
        action();
        tx.commit();
    } catch (const DatabaseError& e) {
        // Only reservation conflicts are normal business errors. Failure to save
        // an exchange result must block the account even for a constraint error.
        if (!e.constraint() || !constraint_is_conflict) blocked_accounts_.insert(account);
        throw;
    }
}
void Store::create_task(const Task& t, Timestamp now) {
    std::lock_guard lock(mutex_);
    required(t.id); required(t.account); required(t.contract);
    write(t.account, [&] {
        auto q = db_.prepare("INSERT INTO tasks VALUES(?,?,?,?,?,?,?)");
        q.bind(1,t.id); q.bind(2,t.account); q.bind(3,t.contract); q.bind(4,name(t.direction));
        q.bind(5,name(t.state));
        q.bind(6,t.state == TaskState::Running ? "running" : t.state == TaskState::Stopped ? "stopped" : "paused");
        q.bind(7,now); q.execute();
    }, true);
}
std::string Store::goal_account(const std::string& id) {
    auto q = db_.prepare("SELECT t.account FROM goals g JOIN tasks t ON t.id=g.task_id WHERE g.id=?");
    q.bind(1,id);
    if (!q.row()) throw RuleError("goal does not exist");
    return q.text(0);
}
void Store::create_goal(const std::string& id, const std::string& task_id, Timestamp now) {
    std::lock_guard lock(mutex_);
    required(id);
    std::string account;
    {
        auto q = db_.prepare("SELECT account FROM tasks WHERE id=?"); q.bind(1,task_id);
        if (!q.row()) throw RuleError("task does not exist");
        account = q.text(0);
    }
    write(account, [&] {
        auto q = db_.prepare("INSERT INTO goals SELECT ?,id,'active',?,? FROM tasks WHERE id=? AND state='running'");
        q.bind(1,id); q.bind(2,now); q.bind(3,now); q.bind(4,task_id); q.execute();
        if (db_.changes() != 1) throw RuleError("only a running task can create a goal");
    }, true);
}
std::optional<OperationRecord> Store::operation(const std::string& id) {
    std::lock_guard lock(mutex_);
    auto q = db_.prepare(std::string(operation_columns) + "WHERE id=?"); q.bind(1,id);
    if (!q.row()) return std::nullopt;
    return record(q);
}
std::vector<OperationRecord> Store::unresolved() {
    std::lock_guard lock(mutex_);
    auto q = db_.prepare(std::string(operation_columns) + "WHERE phase IN ('prepared','sending','unknown') ORDER BY created_at,id");
    std::vector<OperationRecord> records;
    while (q.row()) records.push_back(record(q));
    return records;
}
DispatchResult Store::dispatch(const Operation& op, const std::string& full, Timestamp now,
                               const std::function<OperationResult()>& send,
                               const std::function<Timestamp()>& result_clock) {
    std::unique_lock lock(mutex_);
    required(op.id); required(op.goal_id); required(op.request_digest);
    if (!send) throw RuleError("missing send callback");
    const auto account = goal_account(op.goal_id);
    bool inserted = false;
    write(account, [&] {
        auto old = db_.prepare("SELECT goal_id,kind,request_digest,client_tag,target_order_id FROM operations WHERE id=?");
        old.bind(1,op.id);
        if (old.row()) {
            if (old.text(0)!=op.goal_id || old.text(1)!=name(op.kind) || old.text(2)!=op.request_digest ||
                old.text(3)!=op.client_tag || old.text(4)!=op.target_order_id)
                throw RuleError("Intent ID reused for a different request");
            return;
        }
        auto allowed = db_.prepare("SELECT 1 FROM goals g JOIN tasks t ON t.id=g.task_id WHERE g.id=? AND g.state='active' AND t.state='running'");
        allowed.bind(1,op.goal_id);
        if (!allowed.row()) throw RuleError("task or goal does not allow new operations");
        if (op.kind == OperationKind::Cancel) {
            // Only cancel an order whose creation is recorded under this goal.
            auto owned = db_.prepare("SELECT 1 FROM operations WHERE goal_id=? AND kind<>'cancel' AND exchange_order_id=? AND phase='succeeded'");
            owned.bind(1,op.goal_id); owned.bind(2,op.target_order_id);
            if (op.target_order_id.empty() || !owned.row()) throw RuleError("cancel target ownership not established");
        }
        auto q = db_.prepare("INSERT INTO operations(id,goal_id,kind,request_digest,client_tag,target_order_id,phase,created_at,updated_at) VALUES(?,?,?,?,?,?,'prepared',?,?)");
        q.bind(1,op.id); q.bind(2,op.goal_id); q.bind(3,name(op.kind)); q.bind(4,op.request_digest);
        q.bind(5,op.client_tag); q.bind(6,op.target_order_id); q.bind(7,now); q.bind(8,now); q.execute();
        if (op.kind != OperationKind::Cancel) {
            auto order = db_.prepare("INSERT INTO orders VALUES(?,?,'pending_submit',?)");
            order.bind(1,op.id); order.bind(2,op.id); order.bind(3,now); order.execute();
            auto history = db_.prepare("INSERT INTO state_history(entity_type,entity_id,from_state,to_state,at,reason,source) VALUES('order',?,'','pending_submit',?,'Intent prewrite','local')");
            history.bind(1,op.id); history.bind(2,now); history.execute();
        }
        inserted = true;
    }, true);
    if (!inserted) return {false, *operation(op.id)};
    write(account, [&] {
        auto q = db_.prepare("UPDATE operations SET phase='sending',updated_at=? WHERE id=? AND phase='prepared'");
        q.bind(1,now); q.bind(2,op.id); q.execute();
        if (db_.changes()!=1) throw RuleError("operation already claimed");
        if (op.kind != OperationKind::Cancel)
            order_event(op.id,OrderEvent::Send,EventSource::Local,"dispatch committed",now);
        else {
            auto target = db_.prepare("SELECT id FROM operations WHERE goal_id=? AND exchange_order_id=? AND kind<>'cancel'");
            target.bind(1,op.goal_id); target.bind(2,op.target_order_id);
            if (!target.row()) throw RuleError("cancel target missing");
            order_event(target.text(0),OrderEvent::RequestCancel,EventSource::Local,"cancel dispatch committed",now);
        }
    });
    // Both commits succeeded. S4 must serialize task commands with dispatch.
    // Never hold SQLite's write transaction across a network call.
    lock.unlock();
    OperationResult result;
    try { result = send(); }
    catch (...) { result = {Outcome::Unknown, "", "transport exception; outcome requires reconciliation"}; }
    if (result.outcome == Outcome::Accepted && op.kind != OperationKind::Cancel && result.exchange_order_id.empty())
        result = {Outcome::Unknown,"","incomplete creation response; order identity must be reconciled"};
    if (result.outcome == Outcome::Accepted && op.kind == OperationKind::Cancel && !result.cancellation_confirmed)
        result = {Outcome::Unknown,result.exchange_order_id,"cancel acknowledged; terminal verification required"};
    const Timestamp observed_at = result_clock ? result_clock() :
        std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::system_clock::now().time_since_epoch()).count();
    lock.lock();
    write(account, [&] { finish(op.id,result,full,observed_at); });
    return {true, *operation(op.id)};
}
void Store::finish(const std::string& id, const OperationResult& r, const std::string& full, Timestamp now) {
    if (r.outcome != Outcome::Accepted && r.outcome != Outcome::Rejected && r.outcome != Outcome::Unknown)
        throw RuleError("invalid operation outcome");
    auto current = operation(id);
    if (!current) throw RuleError("operation does not exist");
    if (current->phase == "succeeded" || current->phase == "failed") throw RuleError("operation is already resolved");
    if (r.outcome == Outcome::Accepted) {
        auto kind = db_.prepare("SELECT kind FROM operations WHERE id=?"); kind.bind(1,id); kind.row();
        if (kind.text(0)!="cancel" && r.exchange_order_id.empty()) throw RuleError("accepted create missing order identity");
        if (kind.text(0)=="cancel" && !r.cancellation_confirmed) throw RuleError("cancellation terminal evidence required");
    }
    const std::string phase = r.outcome == Outcome::Accepted ? "succeeded" : r.outcome == Outcome::Rejected ? "failed" : "unknown";
    auto q = db_.prepare("UPDATE operations SET phase=?,exchange_order_id=?,full_intent=?,result=?,updated_at=? WHERE id=?");
    q.bind(1,phase); q.bind(2,r.exchange_order_id); q.bind(3,full); q.bind(4,r.detail); q.bind(5,now); q.bind(6,id); q.execute();
    auto h = db_.prepare("INSERT INTO state_history(entity_type,entity_id,from_state,to_state,at,reason,source) VALUES('operation',?,?,?,?,?,'exchange')");
    h.bind(1,id); h.bind(2,current->phase); h.bind(3,phase); h.bind(4,now);
    h.bind(5,r.detail.empty() ? "operation outcome recorded" : r.detail); h.execute();
    {
        auto kind = db_.prepare("SELECT kind FROM operations WHERE id=?"); kind.bind(1,id); kind.row();
        if (kind.text(0)!="cancel") {
            // A prewrite recovered before sending has no reliable exchange outcome.
            // Query evidence may still prove submission, but never resend it.
            if (order_status(id)==OrderState::PendingSubmit)
                order_event(id,OrderEvent::Send,EventSource::Local,"recovered prewrite; query evidence",now);
            order_event(id,r.outcome==Outcome::Accepted ? OrderEvent::Accepted :
                r.outcome==Outcome::Rejected ? OrderEvent::RejectConfirmed : OrderEvent::OutcomeUnknown,
                EventSource::Exchange,"operation outcome",now);
        } else if (r.outcome==Outcome::Accepted) {
            auto target=db_.prepare("SELECT p.id FROM operations p JOIN operations c ON c.goal_id=p.goal_id AND c.target_order_id=p.exchange_order_id WHERE c.id=? AND p.kind<>'cancel'");
            target.bind(1,id);
            if (!target.row()) throw RuleError("cancel target missing during confirmation");
            order_event(target.text(0),OrderEvent::CancelConfirmed,EventSource::Exchange,"cancellation confirmed",now);
        }
    }
    if (r.outcome != Outcome::Accepted) {
        auto g = db_.prepare("UPDATE goals SET state='paused',updated_at=? WHERE id=?");
        g.bind(1,now); g.bind(2,current->goal_id); g.execute();
        auto t = db_.prepare("UPDATE tasks SET state=CASE WHEN state IN ('stop_pending','pause_pending') THEN state ELSE ? END, updated_at=? WHERE id=(SELECT task_id FROM goals WHERE id=?)");
        t.bind(1,r.outcome == Outcome::Unknown ? "reconciling" : "paused");
        t.bind(2,now); t.bind(3,current->goal_id); t.execute();
    }
}
void Store::resolve(const std::string& id, const OperationResult& result,
                    const std::string& full, const std::string& evidence, Timestamp now) {
    std::lock_guard lock(mutex_);
    if (result.outcome == Outcome::Unknown || evidence.empty()) throw RuleError("affirmative exchange evidence required");
    auto old = operation(id);
    if (!old) throw RuleError("operation does not exist");
    const auto account = goal_account(old->goal_id);
    write(account, [&] {
        finish(id,result,full,now);
        auto q = db_.prepare("INSERT INTO state_history(entity_type,entity_id,from_state,to_state,at,reason,source) VALUES('reconciliation',?,?,?,?,?,'recovery')");
        q.bind(1,id); q.bind(2,old->phase); q.bind(3,result.outcome == Outcome::Accepted ? "succeeded" : "failed");
        q.bind(4,now); q.bind(5,evidence); q.execute();
        // Resolution never automatically resumes a task; S4 schedules recovery.
    });
}
OrderState Store::order_status(const std::string& id) {
    std::lock_guard lock(mutex_);
    auto q = db_.prepare("SELECT state FROM orders WHERE id=?"); q.bind(1,id);
    if (!q.row()) throw RuleError("order does not exist");
    return order_state(q.text(0));
}
void Store::apply_order_event(const std::string& id, OrderEvent event, EventSource source,
                              const std::string& reason, Timestamp now) {
    std::lock_guard lock(mutex_);
    required(reason);
    std::string account;
    {
        auto q = db_.prepare("SELECT t.account FROM orders o JOIN operations p ON p.id=o.operation_id JOIN goals g ON g.id=p.goal_id JOIN tasks t ON t.id=g.task_id WHERE o.id=?");
        q.bind(1,id); if (!q.row()) throw RuleError("order does not exist"); account=q.text(0);
    }
    write(account, [&] {
        order_event(id,event,source,reason,now);
    });
}
void Store::order_event(const std::string& id, OrderEvent event, EventSource source,
                      const std::string& reason, Timestamp now) {
    const auto from = order_status(id);
    const auto to = transition(from,event,source);
    auto q = db_.prepare("UPDATE orders SET state=?,updated_at=? WHERE id=?");
    q.bind(1,name(to)); q.bind(2,now); q.bind(3,id); q.execute();
    auto h = db_.prepare("INSERT INTO state_history(entity_type,entity_id,from_state,to_state,at,reason,source) VALUES('order',?,?,?,?,?,?)");
    h.bind(1,id); h.bind(2,name(from)); h.bind(3,name(to)); h.bind(4,now); h.bind(5,reason); h.bind(6,name(source)); h.execute();
}
void Store::with_all_tasks_stopped(const std::function<void()>& save) {
    std::lock_guard lock(mutex_);
    if (!blocked_accounts_.empty()) throw RuleError("storage fault prevents configuration edits");
    Transaction tx(db_);
    auto q = db_.prepare("SELECT 1 FROM tasks WHERE state<>'stopped' LIMIT 1");
    if (q.row()) throw RuleError("all tasks must be stopped before editing configuration");
    save();
    tx.commit();
}
} // namespace orders2
