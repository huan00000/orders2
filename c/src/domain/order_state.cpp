#include "orders2/domain.h"
#include <array>

namespace orders2 {
namespace {
template<class E, std::size_t N>
std::string_view enum_name(E value, const std::array<std::string_view, N>& names) {
    const auto i = static_cast<std::size_t>(value);
    if (i >= N) throw RuleError("invalid enum value");
    return names[i];
}
constexpr std::array<std::string_view, 10> order_names{
    "pending_submit", "submitting", "submitted", "partially_filled", "filled",
    "cancel_pending", "cancelled", "rejected", "unknown", "reconciling"};
}
std::string_view name(Direction v) {
    return enum_name(v, std::array<std::string_view, 2>{"long", "short"});
}
std::string_view name(GoalState v) {
    return enum_name(v, std::array<std::string_view, 4>{"active", "paused", "completed", "stopped"});
}
std::string_view name(TaskState v) {
    return enum_name(v, std::array<std::string_view, 9>{"stopped", "running", "paused",
        "pause_pending", "stop_pending", "reconciling", "recovery_delay", "awaiting_manual", "storage_blocked"});
}
std::string_view name(OrderState v) { return enum_name(v, order_names); }
std::string_view name(EventSource v) {
    return enum_name(v, std::array<std::string_view, 3>{"local", "exchange", "recovery"});
}
std::string_view name(OperationKind v) {
    return enum_name(v, std::array<std::string_view, 3>{"open", "cancel", "close"});
}
OrderState order_state(std::string_view v) {
    for (std::size_t i = 0; i < order_names.size(); ++i)
        if (order_names[i] == v) return static_cast<OrderState>(i);
    throw RuleError("unknown stored order state");
}
bool terminal(OrderState s) {
    return s == OrderState::Filled || s == OrderState::Cancelled || s == OrderState::Rejected;
}
OrderState transition(OrderState s, OrderEvent e, EventSource source) {
    using S = OrderState;
    using E = OrderEvent;
    const bool remote = source == EventSource::Exchange || source == EventSource::Recovery;
    if (terminal(s)) {
        if (remote && ((s == S::Filled && e == E::Fill) ||
            (s == S::Cancelled && e == E::CancelConfirmed) ||
            (s == S::Rejected && e == E::RejectConfirmed))) return s;
        throw RuleError("terminal order cannot be reopened");
    }
    switch (e) {
    case E::Send:
        if (s == S::PendingSubmit && source == EventSource::Local) return S::Submitting;
        break;
    case E::Accepted:
        if (remote && (s == S::Submitting || s == S::Unknown || s == S::Reconciling || s == S::Submitted))
            return S::Submitted;
        break;
    case E::PartialFill:
        if (remote && s != S::PendingSubmit)
            return s == S::CancelPending ? S::CancelPending : S::PartiallyFilled;
        break;
    case E::Fill:
        if (remote && s != S::PendingSubmit) return S::Filled;
        break;
    case E::RequestCancel:
        if (source == EventSource::Local && (s == S::Submitted || s == S::PartiallyFilled))
            return S::CancelPending;
        break;
    case E::CancelConfirmed:
        if (remote && s != S::PendingSubmit) return S::Cancelled;
        break;
    case E::RejectConfirmed:
        if (remote && (s == S::Submitting || s == S::Unknown || s == S::Reconciling)) return S::Rejected;
        break;
    case E::OutcomeUnknown:
        if (s != S::PendingSubmit) return S::Unknown;
        break;
    case E::BeginReconcile:
        if (s != S::PendingSubmit) return S::Reconciling;
        break;
    }
    throw RuleError("invalid order transition or unverified exchange evidence");
}
} // namespace orders2
