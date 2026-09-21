#pragma once
#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>

namespace orders2 {
using Timestamp = std::int64_t; // UTC milliseconds; injected by the caller.
class RuleError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};
enum class Direction { Long, Short };
enum class GoalState { Active, Paused, Completed, Stopped };
enum class TaskState { Stopped, Running, Paused, PausePending, StopPending,
                       Reconciling, RecoveryDelay, AwaitingManual, StorageBlocked };
enum class OrderState { PendingSubmit, Submitting, Submitted, PartiallyFilled,
                        Filled, CancelPending, Cancelled, Rejected, Unknown, Reconciling };
enum class OrderEvent { Send, Accepted, PartialFill, Fill, RequestCancel,
                        CancelConfirmed, RejectConfirmed, OutcomeUnknown, BeginReconcile };
enum class EventSource { Local, Exchange, Recovery };
enum class OperationKind { Open, Cancel, Close };
enum class Outcome { Accepted, Rejected, Unknown };

std::string_view name(Direction value);
std::string_view name(GoalState value);
std::string_view name(TaskState value);
std::string_view name(OrderState value);
std::string_view name(EventSource value);
std::string_view name(OperationKind value);
OrderState order_state(std::string_view value);
OrderState transition(OrderState from, OrderEvent event, EventSource source);
bool terminal(OrderState value);

struct Task {
    std::string id, account, contract;
    Direction direction{Direction::Long};
    TaskState state{TaskState::Stopped};
};
struct Operation {
    std::string id, goal_id;
    OperationKind kind{OperationKind::Open};
    // Stable digest of canonical request and recovery identifiers, never credentials.
    std::string request_digest, client_tag, target_order_id;
};
struct Goal {
    std::string id, task_id;
    GoalState state{GoalState::Active};
};
struct OperationResult {
    Outcome outcome{Outcome::Unknown};
    std::string exchange_order_id, detail;
    // Cancel HTTP acknowledgements alone never complete an operation.
    bool cancellation_confirmed{false};
};
struct OperationRecord {
    std::string id, goal_id, phase, exchange_order_id, result;
};
struct DispatchResult {
    bool sent{false};
    OperationRecord record;
};
} // namespace orders2
