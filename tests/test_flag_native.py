"""用 Objective-C 测试对象执行生产 Swift 读取函数，不访问真实日历或提醒事项。"""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def swift_function(source, signature):
    start = source.index(signature)
    body = source.index("{", start)
    depth = 1
    end = body + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc"), reason="需要 macOS Swift 编译器")
def test_native_flag_getters_unknown_state_and_write_guard(tmp_path):
    source = (Path(__file__).parents[1] / "checkout/src/telos/connectors/apple/personal_helper.swift").read_text()
    functions = "\n".join(swift_function(source, name) for name in (
        "func remValue(", "func remObject(", "func reminderFlag(", "func requireReadableReminderFlag(",
        "func optionalFields(", "func mergeReadStates("))
    state = swift_function(source, "func state(_ id: String)")
    program = r'''
import Foundation
import ObjectiveC
struct Failure: Error { let code: String; let message: String }
let reminderPrivateFields = ["flagged", "urgent", "attachments", "tags", "early_reminder", "early_reminder_minutes"]
@objc class NumericFlag: NSObject { @objc dynamic var flagged: Int = 0 }
@objc class BooleanFlag: NSObject { @objc dynamic var isFlagged: Bool = false }
@objc class InvalidFlag: NSObject { @objc dynamic var flagged: String = "unknown" }
@objc class Reminder: NSObject {
    @objc dynamic var flaggedContext: NSObject?
    init(_ context: NSObject?) { flaggedContext = context }
}
func extendedReminderState(_ object: NSObject) throws -> [String: Any] { ["urgent": false] }
''' + functions + r'''
struct TestAlerts {
    let object: NSObject
    var rows: [[String: Any]] = []
    func reminder(_ id: String) throws -> NSObject { object }
    func alerts(_ object: NSObject) throws -> [[String: Any]] { rows }
    func tags(_ object: NSObject) throws -> [String] { ["工作"] }
''' + state + r'''
}
for object in [NSObject(), Reminder(nil), Reminder(NSObject()), Reminder(InvalidFlag())] {
    let state = try TestAlerts(object: object).state("test")
    assert(state["flagged"] is NSNull)
    assert(state["flagged_supported"] as? Bool == false)
    assert(state["tags"] as? [String] == ["工作"])
    assert(state["early_reminder"] is NSNull)
    assert(JSONSerialization.isValidJSONObject(state))
    do { try requireReadableReminderFlag(object); fatalError("未知旗标不应允许修改") }
    catch let error as Failure { assert(error.code == "unsupported_private_feature") }
    let early = try TestAlerts(object: object, rows: [["identifier": "a", "dueDateDeltaUnit": 1,
                              "dueDateDeltaCount": -2]]).state("test")
    assert(early["early_reminder_minutes"] as? Int == 120)
}
for flag in [false, true] {
    let numeric = NumericFlag(); numeric.flagged = flag ? 1 : 0
    let boolean = BooleanFlag(); boolean.isFlagged = flag
    for context in [numeric, boolean] as [NSObject] {
        let reminder = Reminder(context)
        let state = try TestAlerts(object: reminder).state("test")
        assert(state["flagged"] as? Bool == flag)
        assert(state["flagged_supported"] as? Bool == true)
        try requireReadableReminderFlag(reminder)
    }
}
let partial = try TestAlerts(object: Reminder(nil), rows: [[:], [:]]).state("test")
assert(partial["early_reminder"] is NSNull)
assert((partial["unavailable_fields"] as? [String])?.contains("early_reminder") == true)
assert(partial["tags"] as? [String] == ["工作"])
print("flag compatibility passed")
'''
    swift = tmp_path / "main.swift"
    swift.write_text(program)
    binary = tmp_path / "flag-test"
    subprocess.run(["swiftc", "-module-cache-path", str(tmp_path / "modules"), str(swift),
                    "-o", str(binary)], check=True, capture_output=True, text=True, timeout=180)
    result = subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=15)
    assert "flag compatibility passed" in result.stdout
