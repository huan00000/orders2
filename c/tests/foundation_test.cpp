#include "orders2/config.h"
#include "orders2/store.h"
#include <filesystem>
#include <cstdlib>
#include <fstream>
#include <functional>
#include <iostream>
#include <iterator>
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>
#include <unistd.h>
#include <sys/stat.h>

using namespace orders2;
namespace {
void check(bool ok, const char* message) { if (!ok) throw std::runtime_error(message); }
template<class E, class F> void raises(F action) {
    try { action(); } catch (const E&) { return; }
    throw std::runtime_error("expected exception was not thrown");
}
struct Sandbox {
    std::filesystem::path path;
    Sandbox() {
        auto pattern=(std::filesystem::temp_directory_path()/"orders2-test-XXXXXX").string();
        std::vector<char> buffer(pattern.begin(),pattern.end()); buffer.push_back('\0');
        auto* created=::mkdtemp(buffer.data());
        if (!created) throw std::runtime_error("mkdtemp failed");
        path=created;
    }
    ~Sandbox() { std::error_code error; std::filesystem::remove_all(path,error); }
    std::string database() const { return (path/"state.sqlite").string(); }
};
void running(Store& store, const std::string& suffix="") {
    store.create_task({"task"+suffix,"account"+suffix,"BTC_USDT",Direction::Long,TaskState::Running},100);
    store.create_goal("goal"+suffix,"task"+suffix,100);
}
Operation op(std::string id="intent", std::string goal="goal") {
    return {id,goal,OperationKind::Open,"test-digest-"+id,"test-tag-"+id,""};
}
OperationResult accepted() { return {Outcome::Accepted,"exchange-42","accepted"}; }
std::int64_t scalar(Database& db, std::string_view sql) {
    auto q=db.prepare(sql); check(q.row(),"missing scalar"); return q.integer(0);
}
std::string text(Database& db, std::string_view sql) {
    auto q=db.prepare(sql); check(q.row(),"missing text"); return q.text(0);
}
std::string read_file(const std::filesystem::path& path) {
    std::ifstream file(path,std::ios::binary);
    return {std::istreambuf_iterator<char>(file),std::istreambuf_iterator<char>()};
}
void state_machine() {
    using S=OrderState; using E=OrderEvent; using Src=EventSource;
    auto s=transition(S::PendingSubmit,E::Send,Src::Local);
    s=transition(s,E::Accepted,Src::Exchange);
    s=transition(s,E::PartialFill,Src::Exchange);
    s=transition(s,E::RequestCancel,Src::Local);
    check(transition(s,E::PartialFill,Src::Exchange)==S::CancelPending,"partial fill lost cancel intent");
    check(transition(s,E::Fill,Src::Exchange)==S::Filled,"fill racing cancellation lost");
    check(transition(s,E::CancelConfirmed,Src::Exchange)==S::Cancelled,"cancel confirmation failed");
    raises<RuleError>([]{ transition(S::CancelPending,E::CancelConfirmed,Src::Local); });
    raises<RuleError>([]{ transition(S::Filled,E::Send,Src::Local); });
    raises<RuleError>([]{ transition(S::PendingSubmit,E::Fill,Src::Exchange); });
    check(transition(S::Unknown,E::BeginReconcile,Src::Recovery)==S::Reconciling,"unknown recovery missing");
    raises<RuleError>([]{ order_state("unexpected exchange code"); });
}
void task_and_goal_constraints() {
    for (auto state : {TaskState::Running,TaskState::Paused,TaskState::PausePending,TaskState::StopPending,
         TaskState::Reconciling,TaskState::RecoveryDelay,TaskState::AwaitingManual,TaskState::StorageBlocked}) {
        Store store(":memory:");
        store.create_task({"first","a","BTC_USDT",Direction::Long,state},1);
        raises<RuleError>([&]{store.with_all_tasks_stopped([]{});});
        raises<DatabaseError>([&]{ store.create_task({"second","a","BTC_USDT",Direction::Long,TaskState::Running},2); });
        check(!store.account_blocked("a"),"uniqueness conflict treated as disk fault");
        store.create_task({"short","a","BTC_USDT",Direction::Short,TaskState::Running},2);
        store.create_task({"other","b","BTC_USDT",Direction::Long,TaskState::Running},2);
    }
    Store store(":memory:"); running(store);
    store.create_task({"old","account","BTC_USDT",Direction::Long,TaskState::Stopped},2);
    raises<DatabaseError>([&]{store.create_goal("second-goal","task",2);});
    raises<RuleError>([&]{store.create_goal("stopped-goal","old",2);});
}
void committed_before_send_and_duplicate() {
    Sandbox box; Store store(box.database()); running(store);
    int sends=0;
    auto send=[&]{
        ++sends;
        Database observer(box.database());
        check(text(observer,"SELECT phase FROM operations WHERE id='intent'")=="sending","prewrite not committed before network");
        check(text(observer,"SELECT full_intent FROM operations WHERE id='intent'").empty(),"full Intent saved before execution");
        check(text(observer,"SELECT state FROM orders WHERE id='intent'")=="submitting","order not linked to dispatch");
        return accepted();
    };
    check(store.dispatch(op(),"complete Intent",101,send,[]{return Timestamp{105};}).sent,"first dispatch not sent");
    auto duplicate=store.dispatch(op(),"complete Intent",102,send);
    check(!duplicate.sent && duplicate.record.exchange_order_id=="exchange-42" && sends==1,"duplicate was sent");
    auto conflict=op(); conflict.request_digest="different";
    raises<RuleError>([&]{store.dispatch(conflict,"",103,send);});
    check(store.order_status("intent")==OrderState::Submitted,"accepted order state missing");
    check(store.unresolved().empty(),"known response still unresolved");
    Database db(box.database());
    check(text(db,"SELECT full_intent FROM operations")=="complete Intent","full Intent not recorded after execution");
    check(scalar(db,"SELECT updated_at FROM operations WHERE id='intent'")==105,"result timestamp reused dispatch time");
    store.dispatch(op("next"),"second operation",104,[]{return OperationResult{Outcome::Accepted,"exchange-43","accepted"};});
    check(scalar(db,"SELECT count(*) FROM goals")==1,"child completion ended parent goal");
}
void unknown_blocks_new_operation() {
    Sandbox box;
    {
        Store store(box.database()); running(store);
        auto r=store.dispatch(op(),"intent",101,[]()->OperationResult {throw std::runtime_error("secret transport detail");});
        check(r.record.phase=="unknown","transport error treated as failed");
        check(r.record.result.find("secret")==std::string::npos,"exception secret leaked");
        raises<RuleError>([&]{store.dispatch(op("next"),"",102,accepted);});
        raises<RuleError>([&]{store.resolve("intent",{Outcome::Unknown,"","not found"},"","",103);});
        check(store.unresolved().size()==1,"unknown missing from recovery scan");
    }
    Store restarted(box.database());
    int sends=0;
    check(!restarted.dispatch(op(),"",104,[&]{++sends; return accepted();}).sent,"restarted unknown resent");
    check(sends==0,"unknown callback called");
    restarted.resolve("intent",accepted(),"intent","exchange detail matched client tag",105);
    check(restarted.unresolved().empty(),"resolution incomplete");
    Database db(box.database());
    check(text(db,"SELECT state FROM tasks")=="reconciling","resolution auto-resumed task");
    check(text(db,"SELECT state FROM goals")=="paused","resolution auto-resumed goal");
}
void explicit_rejection_pauses_parent() {
    Sandbox box; Store store(box.database()); running(store);
    store.dispatch(op(),"intent",101,[]{return OperationResult{Outcome::Rejected,"","rejected by exchange"};});
    check(store.order_status("intent")==OrderState::Rejected,"rejection missing");
    Database db(box.database());
    check(text(db,"SELECT state FROM tasks")=="paused","rejection did not pause task");
    check(text(db,"SELECT state FROM goals")=="paused","rejection did not pause goal");
    raises<RuleError>([&]{store.dispatch(op("next"),"",102,accepted);});
    check(!store.dispatch(op(),"",103,accepted).sent,"failed Intent automatically retried");
}
void storage_failure_before_send() {
    Sandbox box; Store store(box.database()); running(store);
    Database locker(box.database()); locker.exec("BEGIN IMMEDIATE");
    int sends=0;
    raises<DatabaseError>([&]{store.dispatch(op(),"",101,[&]{++sends; return accepted();});});
    locker.exec("ROLLBACK");
    check(sends==0 && store.account_blocked("account"),"prewrite failure allowed network");
    check(!store.operation("intent"),"failed prewrite left partial records");
    raises<RuleError>([&]{store.dispatch(op(),"",102,accepted);});
    running(store,"2");
    store.dispatch(op("other","goal2"),"",102,accepted);
    check(!store.account_blocked("account2"),"unaffected account blocked");
}
void commit_failure_never_sends() {
    Sandbox box; Store store(box.database()); running(store);
    Database db(box.database());
    db.exec("CREATE TABLE deferred_failure(id TEXT REFERENCES goals(id) DEFERRABLE INITIALLY DEFERRED)");
    db.exec("CREATE TRIGGER fail_commit AFTER INSERT ON operations BEGIN INSERT INTO deferred_failure VALUES('missing-goal'); END");
    int sends=0;
    raises<DatabaseError>([&]{store.dispatch(op(),"",101,[&]{++sends;return accepted();});});
    check(sends==0,"failed COMMIT still sent request");
    check(!store.operation("intent"),"failed COMMIT retained prewrite");
    check(scalar(db,"SELECT count(*) FROM orders")==0,"failed COMMIT retained order");
}
void storage_failure_after_send_and_restart() {
    Sandbox box;
    {
        Store store(box.database()); running(store);
        Database locker(box.database());
        int sends=0;
        raises<DatabaseError>([&]{store.dispatch(op(),"",101,[&]{
            ++sends; locker.exec("BEGIN IMMEDIATE"); return accepted();
        });});
        locker.exec("ROLLBACK");
        check(sends==1 && store.account_blocked("account"),"post-send storage fault not latched");
        check(store.operation("intent")->phase=="sending","uncertain send result incorrectly committed");
    }
    Store restarted(box.database());
    int sends=0;
    check(!restarted.dispatch(op(),"",103,[&]{++sends; return accepted();}).sent,"crash window resent");
    check(sends==0 && restarted.unresolved().size()==1,"recovery marker missing");
    // The one-unfinished-operation index must also protect against a different ID.
    raises<DatabaseError>([&]{restarted.dispatch(op("other"),"",104,accepted);});
}
void crash_after_prewrite() {
    Sandbox box;
    {
        Store store(box.database()); running(store);
        Database db(box.database());
        db.exec("INSERT INTO operations(id,goal_id,kind,request_digest,client_tag,target_order_id,phase,created_at,updated_at) VALUES('intent','goal','open','test-digest-intent','test-tag-intent','','prepared',1,1)");
        db.exec("INSERT INTO orders VALUES('intent','intent','pending_submit',1)");
    }
    Store restarted(box.database());
    check(!restarted.dispatch(op(),"",102,accepted).sent,"prepared operation resent after crash");
    restarted.resolve("intent",accepted(),"intent","affirmative exchange response",103);
    check(restarted.order_status("intent")==OrderState::Submitted,"prepared recovery failed");
}
void order_history_atomicity() {
    Sandbox box; Store store(box.database()); running(store);
    store.dispatch(op(),"",101,accepted);
    Database db(box.database());
    const auto previous=scalar(db,"SELECT count(*) FROM state_history");
    db.exec("CREATE TRIGGER history_failure BEFORE INSERT ON state_history BEGIN SELECT RAISE(ABORT,'test history failure'); END");
    raises<DatabaseError>([&]{store.apply_order_event("intent",OrderEvent::Fill,EventSource::Exchange,"fill",102);});
    check(store.order_status("intent")==OrderState::Submitted,"state committed without history");
    check(scalar(db,"SELECT count(*) FROM state_history")==previous,"partial history committed");
    check(store.account_blocked("account"),"failed exchange-state persistence did not block account");
    db.exec("DROP TRIGGER history_failure");
    raises<RuleError>([&]{store.apply_order_event("intent",OrderEvent::Fill,EventSource::Exchange,"fill",103);});
}
void cancellation_is_not_http_success() {
    Store store(":memory:"); running(store);
    store.dispatch(op(),"",101,accepted);
    Operation cancel{"cancel","goal",OperationKind::Cancel,"cancel-digest","","exchange-42"};
    store.dispatch(cancel,"cancel",102,[]{return OperationResult{Outcome::Accepted,"exchange-42","HTTP acknowledged"};});
    check(store.order_status("intent")==OrderState::CancelPending,"HTTP ack finalized cancellation");
    check(store.operation("cancel")->phase=="unknown","HTTP ack released unfinished operation slot");
    raises<RuleError>([&]{store.resolve("cancel",{Outcome::Accepted,"exchange-42","HTTP only"},"cancel","HTTP 200",103);});
    store.apply_order_event("intent",OrderEvent::Fill,EventSource::Exchange,"fill won cancellation race",103);
    check(store.order_status("intent")==OrderState::Filled,"fill during cancel ignored");
    cancel.id="foreign-cancel"; cancel.target_order_id="foreign-order";
    raises<RuleError>([&]{store.dispatch(cancel,"",104,accepted);});
    Store confirmed(":memory:"); running(confirmed);
    confirmed.dispatch(op(),"",101,accepted);
    cancel={"cancel","goal",OperationKind::Cancel,"cancel-digest","","exchange-42"};
    confirmed.dispatch(cancel,"",102,[]{return OperationResult{Outcome::Accepted,"exchange-42","cancelled",true};});
    check(confirmed.order_status("intent")==OrderState::Cancelled,"confirmed cancel missing");
}
void config_validation_and_save() {
    Sandbox box; Store store(box.database());
    const auto file=box.path/".env";
    const std::string original="# preserved comment\nAPI_KEY='fake-one'\nAPI_SECRET=\"fake-secret\"\nCUSTOM=a=b\n";
    save_config(store,file,original);
    check(read_file(file)==original,"configuration text changed");
    auto cfg=load_config(file,[](const std::string& key)->std::optional<std::string>{
        if (key=="API_KEY") return "environment-key";
        return std::nullopt;
    });
    check(cfg.values.at("API_KEY")=="environment-key" && cfg.values.at("CUSTOM")=="a=b","legacy parsing/precedence lost");
    check(cfg.log_retention_days==30 && cfg.scheduled_backup_count==24 && cfg.upgrade_backup_count==5,"defaults incorrect");
    for (const std::string invalid : {"API_KEY=only\n", "API_KEY=a\nAPI_SECRET=b\nPOLL_INTERVAL_SECONDS=nan\n",
         "API_KEY=a\nAPI_SECRET=b\nBACKUP_INTERVAL_SECONDS=0\n", "API_KEY=a\nAPI_KEY=b\nAPI_SECRET=c\n",
         "API_KEY='unclosed\nAPI_SECRET=b\n", "API_KEY=a\nAPI_SECRET=b\nbroken line\n"}) {
        raises<RuleError>([&]{save_config(store,file,invalid);});
        check(read_file(file)==original,"invalid save overwrote original");
    }
    const std::string next="API_KEY=fake-two\nAPI_SECRET=fake-two-secret\n";
    save_config(store,file,next);
    check(read_file(file.string()+".bak")==original,"backup is not previous config");
    save_config(store,file,original);
    check(read_file(file.string()+".bak")==next,"backup rotation incorrect");
    struct stat info{}; check(::stat(file.c_str(),&info)==0 && (info.st_mode&0777)==0600,"configuration mode not private");
    running(store);
    raises<RuleError>([&]{save_config(store,file,next);});
    check(read_file(file)==original,"running task allowed configuration edit");
}
void config_failure_keeps_original() {
    Sandbox box; Store store(box.database()); const auto file=box.path/".env";
    const std::string original="API_KEY=fake\nAPI_SECRET=fake\n";
    save_config(store,file,original);
    std::filesystem::create_directory(file.string()+".bak");
    raises<std::system_error>([&]{save_config(store,file,"API_KEY=next\nAPI_SECRET=next\n");});
    check(read_file(file)==original,"backup failure overwrote original");
    for (const auto& entry : std::filesystem::directory_iterator(box.path))
        check(entry.path().filename().string().find(".tmp.")==std::string::npos,"temporary secret file leaked");
    auto empty=load_config(box.path/"missing",[](const std::string&)->std::optional<std::string>{return std::nullopt;});
    check(!empty.has_credentials(),"empty deployment invented credentials");
}
}
int main() {
    const std::vector<std::pair<const char*,std::function<void()>>> tests{
        {"state machine",state_machine}, {"task and goal constraints",task_and_goal_constraints},
        {"committed prewrite and duplicate",committed_before_send_and_duplicate},
        {"unknown recovery",unknown_blocks_new_operation}, {"explicit rejection",explicit_rejection_pauses_parent},
        {"storage failure before send",storage_failure_before_send},
        {"commit failure never sends",commit_failure_never_sends},
        {"storage failure after send",storage_failure_after_send_and_restart},
        {"prepared crash window",crash_after_prewrite}, {"history atomicity",order_history_atomicity},
        {"cancellation evidence",cancellation_is_not_http_success},
        {"configuration validation",config_validation_and_save}, {"configuration IO failure",config_failure_keeps_original}};
    int failed=0;
    for (const auto& [label,test] : tests) {
        try { test(); std::cout<<"PASS "<<label<<'\n'; }
        catch (const std::exception& e) { ++failed; std::cerr<<"FAIL "<<label<<": "<<e.what()<<'\n'; }
    }
    return failed==0 ? 0 : 1;
}
