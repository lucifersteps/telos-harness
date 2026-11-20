"""Execute production Swift functions with test objects, without opening Apple stores."""
from pathlib import Path
import shutil
import subprocess

import pytest

from test_flag_native import swift_function

pytestmark = pytest.mark.skipif(not shutil.which('swiftc'), reason='Requires Swift compiler')
SOURCE = Path(__file__).parents[1] / 'checkout/src/telos/connectors/apple/personal_helper.swift'


def run_swift(tmp_path, code):
    source = tmp_path / 'main.swift'
    source.write_text(code)
    binary = tmp_path / 'check'
    result = subprocess.run(['swiftc', '-module-cache-path', str(tmp_path / 'modules'),
                             str(source), '-o', str(binary)], capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_attachment_unknown_empty_known_and_write_guard(tmp_path):
    source = SOURCE.read_text()
    functions = '\n'.join(swift_function(source, name) for name in (
        'func remValue(', 'func remCall(', 'func attachmentRows(',
        'func reminderAttachmentState(', 'func requireReadableReminderAttachments('))
    run_swift(tmp_path, r'''
import Foundation
import ObjectiveC
struct Failure: Error { let code: String; let message: String }
func text(_ value: Any?) -> String { value as? String ?? "" }
func remID(_ value: NSObject?) -> String { value?.value(forKey: "identifier") as? String ?? "" }
@objc class Attachment: NSObject {
    @objc dynamic var identifier = "a1"
    @objc dynamic var url: URL? = URL(string: "https://example.com")
    @objc dynamic var fileURL: URL? = nil
}
@objc class Attachments: NSObject { @objc dynamic var attachments: [NSObject] = [] }
@objc class InvalidAttachments: NSObject { @objc dynamic var attachments = "invalid" }
@objc class Reminder: NSObject {
    @objc dynamic var attachmentContext: NSObject?
    init(_ context: NSObject?) { attachmentContext = context }
}
''' + functions + r'''
for object in [NSObject(), Reminder(nil), Reminder(NSObject()), Reminder(InvalidAttachments())] {
    let state = reminderAttachmentState(object)
    assert(state["attachments"] is NSNull && state["attachments_supported"] as? Bool == false)
    assert(JSONSerialization.isValidJSONObject(state))
    do { try requireReadableReminderAttachments(object); fatalError("unknown attachments allowed") }
    catch let error as Failure { assert(error.code == "unsupported_private_feature") }
}
let context = Attachments(), reminder = Reminder(nil)
reminder.attachmentContext = context
assert((reminderAttachmentState(reminder)["attachments"] as? [[String: Any]])?.isEmpty == true)
try requireReadableReminderAttachments(reminder)
context.attachments = [Attachment()]
let state = reminderAttachmentState(reminder)
assert(state["attachments_supported"] as? Bool == true)
assert((state["attachments"] as? [[String: Any]])?.first?["url"] as? String == "https://example.com")
try requireReadableReminderAttachments(reminder)
''')


def test_native_snapshot_and_permission_scope_do_not_touch_other_entity(tmp_path):
    source = SOURCE.read_text()
    functions = '\n'.join(swift_function(source, name) for name in ('func requireAccess(', 'func snapshot('))
    run_swift(tmp_path, r'''
import Foundation
import EventKit
struct Failure: Error { let code: String; let message: String }
func text(_ value: Any?) -> String { value as? String ?? "" }
var eventAllowed = true, reminderAllowed = false, containerFails = false, sectionFails = false, reminderFails = false
struct Source { let sourceIdentifier = "account" }
struct TestCalendar {
    let calendarIdentifier = "list-1", title = "List", allowsContentModifications = true
    let source = Source()
}
struct TestStore { let defaultCalendarForNewEvents: TestCalendar? = TestCalendar() }
let store = TestStore()
var reads: [String] = []
func permitted(_ entity: EKEntityType) -> Bool { entity == .event ? eventAllowed : reminderAllowed }
func permissionName(_ entity: EKEntityType) -> String { permitted(entity) ? "full_access" : "denied" }
func fetchReminders(_ request: [String: Any]) throws -> [Int] {
    reads.append("reminder"); assert(reminderAllowed)
    if reminderFails { throw Failure(code: "read_failed", message: "unavailable reminders") }
    return [1]
}
func reminderPayload(_ value: Int) throws -> [String: Any] { ["kind": "reminder"] }
func fetchEvents(_ request: [String: Any]) throws -> [Int] {
    reads.append("event"); assert(eventAllowed); return [1]
}
func eventPayload(_ value: Int) -> [String: Any] { ["kind": "event"] }
func selectedCalendars(_ request: [String: Any], entity: EKEntityType) throws -> [TestCalendar] {
    assert(permitted(entity)); return [TestCalendar()]
}
func calendarPayload(_ value: TestCalendar, kind: String) -> [String: Any] { ["kind": kind] }
func eventCalendarRows(_ request: [String: Any]) throws -> [[String: Any]] {
    assert(eventAllowed); return [["kind": "event_calendar"]]
}
func sectionRows(_ request: [String: Any]) throws -> [[String: Any]] {
    assert(reminderAllowed)
    if sectionFails { throw Failure(code: "read_failed", message: "sections unavailable") }
    return [["kind": "section"]]
}
struct PersonalReminderContainers {
    init() throws {
        assert(reminderAllowed, "Calendar query instantiated ReminderKit")
        if containerFails { throw Failure(code: "read_failed", message: "lists unavailable") }
    }
    func snapshot(_ request: [String: Any]) throws -> [String: Any] { [:] }
}
''' + functions + r'''
for kind in ["event", "event_calendar"] {
    reads = []
    try requireAccess(["kind": kind])
    let result = try snapshot(["kind": kind])
    assert(reads == (kind == "event" ? ["event"] : []))
    assert((result["containers"] as? [[String: Any]])?.first?["kind"] as? String == "event_calendar")
}
do { try requireAccess([:]); fatalError("Mixed reads require both permissions") }
catch let error as Failure { assert(error.code == "permission_denied" && error.message.contains("denied")) }
let partial = try snapshot([:])
assert((partial["items"] as? [[String: Any]])?.count == 1)
assert((partial["source_errors"] as? [String: Any])?["reminder"] != nil)
eventAllowed = true; reminderAllowed = true; containerFails = true; reminderFails = true
let independent = try snapshot([:])
let containers = independent["containers"] as! [[String: Any]]
assert(containers.contains { $0["kind"] as? String == "event_calendar" })
assert(containers.contains { $0["kind"] as? String == "section" })
assert(containers.first { $0["kind"] as? String == "list" }?["item_count"] is NSNull)
assert((independent["source_errors"] as? [String: Any])?["group"] != nil)
assert((independent["source_errors"] as? [String: Any])?["reminder"] != nil)
reminderFails = false; sectionFails = true
let list = try snapshot(["kind": "list"])
assert((list["containers"] as? [[String: Any]])?.contains { $0["kind"] as? String == "list" } == true)
assert((list["source_errors"] as? [String: Any])?["section"] != nil)
eventAllowed = false; reminderAllowed = true; reads = []
try requireAccess(["kind": "reminder"])
_ = try snapshot(["kind": "reminder"])
assert(reads == ["reminder"])
do { try requireAccess(["kind": "event"]); fatalError("Denied event permission bypassed") }
catch let error as Failure { assert(error.code == "permission_denied") }
''')


def test_optional_field_groups_fail_independently_and_unknowns_guard_writes(tmp_path):
    source = SOURCE.read_text()
    functions = '\n'.join(swift_function(source, name) for name in (
        'func optionalFields(', 'func mergeReadStates(', 'func extendedReminderState(',
        'func requireReadableWriteFields(', 'func remValue(', 'func remObject(',
        'func remCall(', 'func reminderFlag('))
    state = swift_function(source, 'func state(_ id: String)')
    constants = source[source.index('let reminderPrivateFields ='):source.index('\nfunc optionalFields(')]
    run_swift(tmp_path, r'''
import Foundation
import ObjectiveC
struct Failure: Error { let code: String; let message: String }
func text(_ value: Any?) -> String { value as? String ?? "" }
func remID(_ value: NSObject?) -> String { (value as? Identified)?.identifier ?? "" }
@objc class Identified: NSObject { @objc dynamic var identifier = "known" }
@objc class Flag: NSObject { @objc dynamic var flagged = true }
@objc class Urgent: NSObject { @objc dynamic var isUrgentStateEnabledForCurrentUser = false }
@objc class Reminder: Identified {
    @objc dynamic var parentReminder: NSObject? = nil
    @objc dynamic var subtaskContext: NSObject? = NSObject()
    @objc dynamic var list: NSObject? = NSObject()
    @objc dynamic var flaggedContext: NSObject? = Flag()
    @objc dynamic var urgentAlarmContext: NSObject? = Urgent()
}
var childrenFail = false, sectionFail = false
func remFetch(_ object: NSObject, _ name: String) throws -> AnyObject {
    if childrenFail { throw Failure(code: "read_failed", message: "children") }
    return [] as NSArray
}
func sectionMemberships(_ object: NSObject) throws -> [String: String] {
    if sectionFail { throw Failure(code: "read_failed", message: "sections") }
    return [:]
}
func reminderAttachmentState(_ object: NSObject) -> [String: Any] {
    ["attachments": NSNull(), "attachments_supported": false]
}
''' + constants + functions + r'''
struct TestAlerts {
    let object: NSObject
    var tagsFail = false
    var rows: [[String: Any]] = []
    func reminder(_ id: String) throws -> NSObject { object }
    func tags(_ object: NSObject) throws -> [String] {
        if tagsFail { throw Failure(code: "read_failed", message: "tags") }
        return ["Work"]
    }
    func alerts(_ object: NSObject) throws -> [[String: Any]] { rows }
''' + state + r'''
}
let reminder = Reminder()
var value = try TestAlerts(object: reminder, tagsFail: true).state("test")
assert(value["tags"] is NSNull && value["flagged"] as? Bool == true)
assert(value["parent_id"] as? String == "" && (value["subtask_ids"] as? [String]) == [])
assert(value["section_id"] as? String == "" && value["urgent"] as? Bool == false)
assert(Set(value["unavailable_fields"] as! [String]) == Set(["tags", "attachments"]))
childrenFail = true; sectionFail = true
value = try TestAlerts(object: reminder).state("test")
assert(value["tags"] as? [String] == ["Work"] && value["flagged"] as? Bool == true)
assert(Set(value["unavailable_fields"] as! [String]) == Set(["subtask_ids", "section_id", "attachments"]))
for rows: [[String: Any]] in [[[:], [:]], [["dueDateDeltaUnit": 99, "dueDateDeltaCount": -1]],
    [["dueDateDeltaUnit": 1, "dueDateDeltaCount": Int.min]]] {
    let state = try TestAlerts(object: reminder, rows: rows).state("test")
    assert(state["early_reminder"] is NSNull)
    assert((state["unavailable_fields"] as! [String]).contains("early_reminder"))
    assert(state["flagged"] as? Bool == true)
}
let absent = try extendedReminderState(NSObject())
assert(absent["parent_id"] is NSNull && absent["subtask_ids"] is NSNull)
var unknown = optionalFields(reminderPrivateFields) { throw Failure(code: "read_failed", message: "ReminderKit") }
assert(JSONSerialization.isValidJSONObject(unknown))
assert(unknown["flagged_supported"] as? Bool == false && unknown["attachments_supported"] as? Bool == false)
unknown["kind"] = "reminder"
try requireReadableWriteFields(unknown, fields: ["title": "safe title"], action: "update")
for fields: [String: Any] in [["tags": []], ["due": "2026-12-01"], ["completed": true],
    ["parent_id": ""], ["section_id": ""], ["calendar_id": "new"], ["early_reminder_minutes": NSNull()]] {
    do { try requireReadableWriteFields(unknown, fields: fields, action: "update"); fatalError("unsafe write") }
    catch let error as Failure { assert(error.code == "unsupported_private_feature") }
}
do { try requireReadableWriteFields(unknown, fields: [:], action: "delete"); fatalError("unsafe delete") }
catch let error as Failure { assert(error.code == "unsupported_private_feature") }
''')
