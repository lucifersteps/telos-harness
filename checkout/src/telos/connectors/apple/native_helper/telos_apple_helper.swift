import AppKit
import EventKit
import Foundation
import ObjectiveC
import SQLite3

let store = EKEventStore()
let isoFormatter: ISO8601DateFormatter = {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter
}()
let isoParser = ISO8601DateFormatter()
let urgentAlarmSoundName = "Alarm"

func main() {
    do {
        let input = FileHandle.standardInput.readDataToEndOfFile()
        let request = try decodeObject(input)
        let action = (CommandLine.arguments.dropFirst().first as String?) ?? string(request["action"])
        let data: [String: Any]
        switch action {
        case "capabilities":
            data = capabilities()
        case "list":
            let filters = object(request["filters"])
            try requestAccessForList(filters: filters)
            data = try listItems(filters: filters)
        case "detail":
            try requestAccess(reminders: true, events: false)
            try? requestAccess(entity: .event)
            data = detail(ids: stringArray(request["ids"]))
        case "create":
            let items = objectArray(request["items"])
            try requestAccessForCreate(items: items)
            data = createItems(items: items)
        case "update":
            let updates = objectArray(request["updates"])
            try requestAccessForUpdates(updates: updates)
            data = updateItems(updates: updates)
        case "delete":
            try requestAccess(reminders: true, events: false)
            try? requestAccess(entity: .event)
            data = deleteItems(ids: stringArray(request["ids"]), force: bool(request["force"], defaultValue: false))
        default:
            throw HelperError("unsupported action: \(action)", code: "unsupported_action")
        }
        writeJSON(["ok": true, "data": data])
    } catch let error as HelperError {
        writeJSON([
            "ok": false,
            "error": [
                "message": error.message,
                "code": error.code,
                "retriable": error.retriable,
                "remediation": error.remediation,
            ],
        ])
        exit(error.exitCode)
    } catch {
        writeJSON([
            "ok": false,
            "error": [
                "message": String(describing: error),
                "code": "native_apple_helper_error",
            ],
        ])
        exit(1)
    }
}

struct HelperError: Error {
    let message: String
    let code: String
    let retriable: Bool
    let remediation: String
    let exitCode: Int32

    init(
        _ message: String,
        code: String = "native_apple_error",
        retriable: Bool = false,
        remediation: String = "",
        exitCode: Int32 = 1
    ) {
        self.message = message
        self.code = code
        self.retriable = retriable
        self.remediation = remediation
        self.exitCode = exitCode
    }
}

func capabilities() -> [String: Any] {
    let hasPrivateReminders = PrivateReminderKitBridge.isAvailable
    let hasPrivateReminderAttachments = PrivateReminderKitBridge.supportsAttachments
    let hasSQLiteReads = SQLiteReminderSnapshot.findDatabasePath() != nil
    let hasRuntimeAttachments = runtimeSupports(EKCalendarItem.self, selectorName: "attachments")
        && runtimeSupports(EKCalendarItem.self, selectorName: "setAttachments:")
    let hasTravelTime = runtimeSupports(EKEvent.self, selectorName: "travelTime")
        && runtimeSupports(EKEvent.self, selectorName: "setTravelTime:")
    return [
        "eventkit": [
            "reminder_lists": true,
            "reminder_crud": true,
            "calendar_crud": true,
            "alarms": true,
            "recurrence": true,
            "url": true,
            "priority": true,
            "structured_location": true,
            "attachments": hasRuntimeAttachments,
            "travel_time": hasTravelTime,
            "audio_alarms": true,
        ],
        "private_reminders": [
            "sections": hasPrivateReminders,
            "tags": hasPrivateReminders,
            "subtasks": hasPrivateReminders,
            "flagged": hasPrivateReminders,
            "attachments": hasPrivateReminderAttachments,
            "urgent": true,
        ],
        "sqlite_reads": [
            "high_fidelity_reminders": hasSQLiteReads,
            "sections": hasSQLiteReads,
            "tags": hasSQLiteReads,
            "subtasks": hasSQLiteReads,
        ],
    ]
}

func listItems(filters: [String: Any]) throws -> [String: Any] {
    let listIDs = Set(stringArray(filters["list_ids"]))
    let calendarIDs = Set(stringArray(filters["calendar_ids"]))
    let sectionIDs = Set(stringArray(filters["section_ids"]).map(normalizedIdentifier))
    let parentIDs = Set(stringArray(filters["parent_ids"]).map(normalizedIdentifier))
    let requiredTags = Set(stringArray(filters["tags"]).map { $0.lowercased() })
    let flaggedFilter = filters["flagged"] == nil ? nil : bool(filters["flagged"], defaultValue: false)
    let urgentFilter = filters["urgent"] == nil ? nil : bool(filters["urgent"], defaultValue: false)
    let kind = string(filters["kind"]).lowercased()
    let includeReminders = kind != "event"
    let includeEvents = kind != "reminder"
    let dueStart = parseDate(filters["due_start"])
    let dueEnd = parseDate(filters["due_end"])
    let priorityFilter = filters["priority"] == nil ? nil : Int(int64(filters["priority"]))
    let includeOverdue = bool(filters["include_overdue"], defaultValue: true)
    let allReminderCalendars = store.calendars(for: .reminder)
    let allEventCalendars = store.calendars(for: .event)
    let reminderCalendars = listIDs.isEmpty
        ? allReminderCalendars
        : allReminderCalendars.filter { listIDs.contains($0.calendarIdentifier) }
    let eventCalendars = calendarIDs.isEmpty
        ? allEventCalendars
        : allEventCalendars.filter { calendarIDs.contains($0.calendarIdentifier) }
    let completed = bool(filters["completed"], defaultValue: false)
    let text = string(filters["text"]).lowercased()
    let snapshot = includeReminders
        ? SQLiteReminderSnapshot.load(calendarListIDsByTitle: listIDMap(reminderCalendars))
        : .empty

    let serializedReminders = includeReminders
        ? try fetchReminders(completed: completed, calendars: reminderCalendars)
            .filter { reminder in reminderMatchesText(reminder, text: text) }
            .map { serializeReminder($0) }
        : []
    let eventKitIDByReminderKitID = reminderEventKitIDMap(serializedReminders, snapshot: snapshot)
    let reminders = serializedReminders
        .map { enrichReminderPayload($0, snapshot: snapshot, eventKitIDByReminderKitID: eventKitIDByReminderKitID) }
        .filter { payload in
            if !reminderMatchesDue(payload, start: dueStart, end: dueEnd) {
                return false
            }
            if !includeOverdue
                && !bool(payload["completed"], defaultValue: false)
                && reminderIsOverdue(payload, now: Date()) {
                return false
            }
            if !reminderMatchesPriority(payload, priorityFilter: priorityFilter) {
                return false
            }
            if !sectionIDs.isEmpty {
                let sectionID = normalizedIdentifier(string(payload["section_id"]))
                if sectionID.isEmpty || !sectionIDs.contains(sectionID) {
                    return false
                }
            }
            if !parentIDs.isEmpty {
                let parentID = normalizedIdentifier(string(payload["parent_id"]))
                if parentID.isEmpty || !parentIDs.contains(parentID) {
                    return false
                }
            }
            if !requiredTags.isEmpty {
                let tags = Set(stringArray(payload["tags"]).map { $0.lowercased() })
                if !requiredTags.isSubset(of: tags) {
                    return false
                }
            }
            if let flaggedFilter = flaggedFilter, bool(payload["flagged"], defaultValue: false) != flaggedFilter {
                return false
            }
            if let urgentFilter = urgentFilter, bool(payload["urgent"], defaultValue: false) != urgentFilter {
                return false
            }
            return true
        }

    let events: [[String: Any]]
    if includeEvents {
        let eventRange = eventDateRange(filters: filters)
        let eventPredicate = store.predicateForEvents(
            withStart: eventRange.start,
            end: eventRange.end,
            calendars: eventCalendars
        )
        events = store.events(matching: eventPredicate)
            .filter { event in eventMatchesText(event, text: text) }
            .map { serializeEvent($0) }
    } else {
        events = []
    }

    return [
        "reminder_lists": includeReminders ? reminderCalendars.map(serializeCalendar) : [],
        "event_calendars": includeEvents ? eventCalendars.map(serializeCalendar) : [],
        "sections": includeReminders
            ? snapshot.sections.filter { section in
                listIDs.isEmpty || listIDs.contains(string(section["list_id"]))
            }
            : [],
        "reminders": reminders,
        "events": events,
    ]
}

func detail(ids: [String]) -> [String: Any] {
    var items: [[String: Any]] = []
    var missing: [String] = []
    let reminderCalendars = store.calendars(for: .reminder)
    let snapshot = SQLiteReminderSnapshot.load(calendarListIDsByTitle: listIDMap(reminderCalendars))
    let allReminders = (try? fetchReminders(completed: false, calendars: reminderCalendars)) ?? []
    let completedReminders = (try? fetchReminders(completed: true, calendars: reminderCalendars)) ?? []
    let serializedReminders = (allReminders + completedReminders).map { serializeReminder($0) }
    let eventKitIDByReminderKitID = reminderEventKitIDMap(serializedReminders, snapshot: snapshot)
    for id in ids {
        if let section = snapshot.section(for: id) {
            items.append(section)
        } else if let item = store.calendarItem(withIdentifier: id) {
            if let reminder = item as? EKReminder {
                items.append(enrichReminderPayloadWithSubtasks(
                    serializeReminder(reminder, detail: true),
                    candidates: allReminders + completedReminders,
                    snapshot: snapshot,
                    eventKitIDByReminderKitID: eventKitIDByReminderKitID
                ))
            } else if let event = item as? EKEvent {
                items.append(serializeEvent(event, detail: true))
            } else {
                missing.append(id)
            }
        } else if let reminder = reminderByAnyIdentifier(id, candidates: allReminders + completedReminders, snapshot: snapshot) {
            items.append(enrichReminderPayloadWithSubtasks(
                serializeReminder(reminder, detail: true),
                candidates: allReminders + completedReminders,
                snapshot: snapshot,
                eventKitIDByReminderKitID: eventKitIDByReminderKitID
            ))
        } else if let event = store.event(withIdentifier: id) {
            items.append(serializeEvent(event, detail: true))
        } else if let calendar = store.calendar(withIdentifier: id) {
            items.append(serializeCalendar(calendar))
        } else {
            missing.append(id)
        }
    }
    return ["items": items, "missing": missing]
}

func createItems(items: [[String: Any]]) -> [String: Any] {
    var created: [[String: Any]] = []
    var failed: [[String: Any]] = []
    for item in items {
        do {
            switch string(item["type"]) {
            case "reminder_list":
                created.append(try createReminderList(item))
            case "reminder":
                created.append(try createReminder(item))
            case "event_calendar":
                created.append(try createEventCalendar(item))
            case "event":
                created.append(try createEvent(item))
            case "section":
                created.append(try privateReminderBridge().createSection(item: item, eventStore: store))
            default:
                throw HelperError("unsupported create type: \(string(item["type"]))", code: "unsupported_item_type")
            }
        } catch {
            failed.append(["item": item, "error": String(describing: error)])
        }
    }
    return ["created": created, "failed": failed]
}

func updateItems(updates: [[String: Any]]) -> [String: Any] {
    var updated: [[String: Any]] = []
    var failed: [[String: Any]] = []
    for update in updates {
        let id = string(update["id"])
        var fields = updateFields(update)
        let clears = stringArray(update["clear"])
        do {
            guard !id.isEmpty else {
                throw HelperError("id is required", code: "invalid_update")
            }
            try applyClearFields(&fields, clears: clears)
            let calendarItem = store.calendarItem(withIdentifier: id)
            if string(update["type"]) == "section" || calendarItem == nil {
                if let calendar = store.calendar(withIdentifier: id) {
                    try applyCalendarFields(calendar, fields: fields)
                    try store.saveCalendar(calendar, commit: true)
                    updated.append(serializeCalendar(calendar))
                    continue
                }
                if (string(update["type"]) == "section" || PrivateReminderKitBridge.isAvailable),
                   let section = try privateReminderBridge().updateSection(id: id, fields: fields) {
                    updated.append(section)
                    continue
                }
            }
            guard let item = calendarItem else {
                throw HelperError("Apple item not found: \(id)", code: "not_found")
            }
            if let reminder = item as? EKReminder {
                try requirePrivateReminderSupport(fields)
                try applyReminderFields(reminder, fields: fields)
                try store.save(reminder, commit: true)
                if privateReminderFieldsRequested(fields) {
                    try privateReminderBridge().patchReminder(candidateIDs: reminderIdentifierCandidates(reminder), fields: fields)
                }
                updated.append(serializeReminderWithCurrentSnapshot(reminder, detail: true))
            } else if let event = item as? EKEvent {
                try applyEventFields(event, fields: fields)
                try store.save(event, span: updateSpan(update, fields: fields), commit: true)
                updated.append(serializeEvent(event, detail: true))
            } else {
                throw HelperError("unsupported item type for update", code: "unsupported_item_type")
            }
        } catch {
            failed.append(["id": id, "error": String(describing: error)])
        }
    }
    return ["updated": updated, "failed": failed]
}

func deleteItems(ids: [String], force: Bool) -> [String: Any] {
    var deleted: [String] = []
    var missing: [String] = []
    var failed: [[String: Any]] = []
    let reminderCalendars = store.calendars(for: .reminder)
    let snapshot = SQLiteReminderSnapshot.load(calendarListIDsByTitle: listIDMap(reminderCalendars))
    for id in ids {
        do {
            if snapshot.section(for: id) != nil {
                if !force && snapshot.sectionHasReminders(id) {
                    throw HelperError("section is not empty; pass force=true to delete it", code: "container_not_empty")
                }
                try privateReminderBridge().deleteSection(id: id)
                deleted.append(id)
            } else if let item = store.calendarItem(withIdentifier: id) {
                if let reminder = item as? EKReminder {
                    if !force && snapshot.reminderHasSubtasks(id) {
                        throw HelperError("reminder has subtasks; pass force=true to delete it", code: "container_not_empty")
                    }
                    try store.remove(reminder, commit: true)
                    deleted.append(id)
                } else if let event = item as? EKEvent {
                    try store.remove(event, span: deleteSpan(force: force), commit: true)
                    deleted.append(id)
                } else {
                    failed.append(["id": id, "error": "unsupported item type"])
                }
            } else if let event = store.event(withIdentifier: id) {
                try store.remove(event, span: deleteSpan(force: force), commit: true)
                deleted.append(id)
            } else if let calendar = store.calendar(withIdentifier: id) {
                if !force && calendarContainsItems(calendar) {
                    throw HelperError("calendar/list is not empty; pass force=true to delete it", code: "container_not_empty")
                }
                try store.removeCalendar(calendar, commit: true)
                deleted.append(id)
            } else {
                missing.append(id)
            }
        } catch {
            failed.append(["id": id, "error": String(describing: error)])
        }
    }
    return ["deleted": deleted, "missing": missing, "failed": failed]
}

func createReminderList(_ item: [String: Any]) throws -> [String: Any] {
    let calendar = EKCalendar(for: .reminder, eventStore: store)
    calendar.title = try requiredTitle(item)
    calendar.source = try defaultSource(for: .reminder)
    if let color = try calendarColor(from: item["color"]) {
        calendar.color = color
    }
    try store.saveCalendar(calendar, commit: true)
    return serializeCalendar(calendar)
}

func createEventCalendar(_ item: [String: Any]) throws -> [String: Any] {
    let calendar = EKCalendar(for: .event, eventStore: store)
    calendar.title = try requiredTitle(item)
    calendar.source = try defaultSource(for: .event)
    if let color = try calendarColor(from: item["color"]) {
        calendar.color = color
    }
    try store.saveCalendar(calendar, commit: true)
    return serializeCalendar(calendar)
}

func createReminder(_ item: [String: Any]) throws -> [String: Any] {
    if !string(item["parent_id"]).isEmpty {
        return try privateReminderBridge().createSubtask(item: item)
    }
    try requirePrivateReminderSupport(item)
    let reminder = EKReminder(eventStore: store)
    try applyReminderFields(reminder, fields: item)
    if reminder.title.isEmpty {
        reminder.title = try requiredTitle(item)
    }
    reminder.calendar = reminderCalendar(id: string(item["list_id"]))
    try store.save(reminder, commit: true)
    do {
        if privateReminderFieldsRequested(item) {
            try privateReminderBridge().patchReminder(candidateIDs: reminderIdentifierCandidates(reminder), fields: item)
        }
    } catch {
        try? store.remove(reminder, commit: true)
        throw error
    }
    return serializeReminderWithCurrentSnapshot(reminder, detail: true)
}

func createEvent(_ item: [String: Any]) throws -> [String: Any] {
    let event = EKEvent(eventStore: store)
    try applyEventFields(event, fields: item)
    if event.title.isEmpty {
        event.title = try requiredTitle(item)
    }
    event.calendar = eventCalendar(id: string(item["calendar_id"]))
    if event.startDate == nil {
        throw HelperError("start_at is required for event", code: "invalid_event")
    }
    if event.endDate == nil {
        throw HelperError("end_at is required for event", code: "invalid_event")
    }
    try store.save(event, span: .thisEvent, commit: true)
    return serializeEvent(event, detail: true)
}

func applyReminderFields(_ reminder: EKReminder, fields: [String: Any]) throws {
    if let value = fields["title"] {
        let title = string(value)
        if title.isEmpty {
            throw HelperError("title cannot be cleared", code: "invalid_title")
        }
        reminder.title = title
    }
    if let value = fields["notes"] { reminder.notes = string(value) }
    if let value = fields["url"] { reminder.url = URL(string: string(value)) }
    if let value = fields["priority"] { reminder.priority = reminderPriority(value) }
    if let value = fields["completed"] { reminder.isCompleted = bool(value, defaultValue: false) }
    if let value = fields["list_id"], !string(value).isEmpty { reminder.calendar = reminderCalendar(id: string(value)) }
    if fields.keys.contains("alarms") {
        replaceAlarms(reminder, alarms: [])
    }
    if fields.keys.contains("recurrence") {
        for rule in reminder.recurrenceRules ?? [] {
            reminder.removeRecurrenceRule(rule)
        }
    }
    if fields.keys.contains("due") {
        let due = string(fields["due"])
        reminder.dueDateComponents = due.isEmpty ? nil : dateComponents(from: due)
    }
    if fields.keys.contains("urgent") {
        removeUrgentAlarms(reminder)
    }
}

func applyEventFields(_ event: EKEvent, fields: [String: Any]) throws {
    if let value = fields["title"] {
        let title = string(value)
        if title.isEmpty {
            throw HelperError("title cannot be cleared", code: "invalid_title")
        }
        event.title = title
    }
    if let value = fields["notes"] { event.notes = string(value) }
    if let value = fields["url"] { event.url = URL(string: string(value)) }
    if let value = fields["location"] { event.location = string(value) }
    if let value = fields["all_day"] { event.isAllDay = bool(value, defaultValue: false) }
    if let value = fields["calendar_id"], !string(value).isEmpty { event.calendar = eventCalendar(id: string(value)) }
    if let value = fields["start_at"], let date = parseDate(string(value)) { event.startDate = date }
    if let value = fields["end_at"], let date = parseDate(string(value)) { event.endDate = date }
    if let value = fields["alarms"] { replaceAlarms(event, alarms: objectArray(value)) }
    if let value = fields["recurrence"] { replaceRecurrence(event, recurrence: object(value)) }
    if fields.keys.contains("travel_time_minutes") {
        try setTravelTime(event, fields: fields)
    }
    if fields.keys.contains("attachments") {
        try replaceAttachments(event, attachments: objectArray(fields["attachments"]))
    }
}

func applyCalendarFields(_ calendar: EKCalendar, fields: [String: Any]) throws {
    if let value = fields["title"] { calendar.title = string(value) }
    if fields.keys.contains("color") {
        let colorText = string(fields["color"])
        if colorText.isEmpty {
            throw HelperError("color cannot be empty", code: "invalid_color")
        }
        calendar.color = try calendarColor(from: colorText)
    }
}

func updateFields(_ update: [String: Any]) -> [String: Any] {
    var fields = object(update["set"])
    for key in appleWritableFieldNames() {
        guard update.keys.contains(key) else {
            continue
        }
        if isNull(update[key]) {
            if let cleared = clearedFieldValue(for: key) {
                fields[key] = cleared
            }
        } else {
            fields[key] = update[key] as Any
        }
    }
    return fields
}

func appleWritableFieldNames() -> Set<String> {
    [
        "title",
        "color",
        "list_id",
        "section_id",
        "calendar_id",
        "parent_id",
        "notes",
        "url",
        "due",
        "completed",
        "flagged",
        "urgent",
        "priority",
        "tags",
        "subtasks",
        "start_at",
        "end_at",
        "all_day",
        "location",
        "travel_time_minutes",
        "alarms",
        "recurrence",
        "attachments",
    ]
}

func clearedFieldValue(for key: String) -> Any? {
    switch key {
    case "notes", "url", "location", "due", "priority", "section_id", "parent_id":
        return ""
    case "alarms", "tags", "subtasks", "attachments":
        return []
    case "recurrence":
        return [:]
    case "completed", "flagged", "urgent", "all_day":
        return false
    case "travel_time_minutes":
        return 0
    default:
        return nil
    }
}

func applyClearFields(_ fields: inout [String: Any], clears: [String]) throws {
    for raw in clears {
        let key = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if key.isEmpty {
            continue
        }
        guard let value = clearedFieldValue(for: key) else {
            throw HelperError("unsupported clear field: \(key)", code: "invalid_update")
        }
        fields[key] = value
    }
}

func serializeCalendar(_ calendar: EKCalendar) -> [String: Any] {
    return compact([
        "id": calendar.calendarIdentifier,
        "title": calendar.title,
        "color": calendar.color.hexString,
        "allows_content_modifications": calendar.allowsContentModifications,
    ])
}

func serializeReminder(_ reminder: EKReminder, detail: Bool = false) -> [String: Any] {
    var payload: [String: Any] = [
        "id": reminder.calendarItemIdentifier,
        "external_identifier": reminder.calendarItemExternalIdentifier ?? "",
        "list_id": reminder.calendar.calendarIdentifier,
        "title": reminder.title ?? "",
        "completed": reminder.isCompleted,
        "priority": reminder.priority,
        "urgent": hasUrgentAlarm(reminder),
    ]
    if let due = reminder.dueDateComponents {
        let serialized = serializeDateComponents(due)
        payload["due"] = serialized.hasTime ? serialized.dateTime : serialized.date
    }
    if detail {
        payload["notes"] = reminder.notes ?? ""
        payload["url"] = reminder.url?.absoluteString ?? ""
        payload["last_modified_at"] = formatDate(reminder.lastModifiedDate)
        payload["created_at"] = formatDate(reminder.creationDate)
        payload["alarms"] = serializeAlarms(reminder.alarms, urgentDueDate: reminderDueDate(reminder))
        payload["recurrence"] = serializeRecurrenceRules(reminder.recurrenceRules)
        let privateAttachments = serializePrivateReminderAttachments(reminder)
        payload["attachments"] = privateAttachments.isEmpty ? serializeAttachments(reminder) : privateAttachments
    }
    return compact(payload)
}

func serializeReminderWithCurrentSnapshot(_ reminder: EKReminder, detail: Bool = false) -> [String: Any] {
    store.refreshSourcesIfNecessary()
    let current = (store.calendarItem(withIdentifier: reminder.calendarItemIdentifier) as? EKReminder) ?? reminder
    let reminderCalendars = store.calendars(for: .reminder)
    let snapshot = SQLiteReminderSnapshot.load(calendarListIDsByTitle: listIDMap(reminderCalendars))
    let seed = [serializeReminder(current)]
    let eventKitIDByReminderKitID = reminderEventKitIDMap(seed, snapshot: snapshot)
    return enrichReminderPayload(
        serializeReminder(current, detail: detail),
        snapshot: snapshot,
        eventKitIDByReminderKitID: eventKitIDByReminderKitID
    )
}

func serializeEvent(_ event: EKEvent, detail: Bool = false) -> [String: Any] {
    var payload: [String: Any] = [
        "id": event.calendarItemIdentifier,
        "event_identifier": event.eventIdentifier ?? "",
        "calendar_item_identifier": event.calendarItemIdentifier,
        "external_identifier": event.calendarItemExternalIdentifier ?? "",
        "calendar_id": event.calendar.calendarIdentifier,
        "title": event.title ?? "",
        "start_at": formatDate(event.startDate),
        "end_at": formatDate(event.endDate),
        "all_day": event.isAllDay,
    ]
    if let travelTime = getTravelTime(event), travelTime > 0 {
        payload["travel_time_minutes"] = travelTime / 60.0
    }
    if detail {
        payload["notes"] = event.notes ?? ""
        payload["url"] = event.url?.absoluteString ?? ""
        payload["location"] = event.location ?? ""
        payload["availability"] = event.availability.rawValue
        payload["status"] = event.status.rawValue
        payload["last_modified_at"] = formatDate(event.lastModifiedDate)
        payload["created_at"] = formatDate(event.creationDate)
        payload["attendees"] = event.attendees?.map(serializeParticipant) ?? []
        payload["alarms"] = serializeAlarms(event.alarms)
        payload["recurrence"] = serializeRecurrenceRules(event.recurrenceRules)
        payload["attachments"] = serializeAttachments(event)
        payload["allows_travel_time_modifications"] = allowsTravelTimeModifications(event)
        payload["duration_including_travel_seconds"] = getDouble(event, selectorName: "durationIncludingTravel")
        payload["start_at_including_travel"] = formatDate(getDate(event, selectorName: "startDateIncludingTravel"))
    }
    return compact(payload)
}

func serializeAlarms(_ alarms: [EKAlarm]?, urgentDueDate: Date? = nil) -> [[String: Any]] {
    (alarms ?? []).map { alarm in
        var payload = compact([
            "absolute_at": formatDate(alarm.absoluteDate),
            "sound_name": alarm.soundName ?? "",
            "type": alarm.type.rawValue,
            "urgent": isUrgentAlarm(alarm) || isDueDateAlarm(alarm, dueDate: urgentDueDate),
        ])
        if alarm.relativeOffset < 0 {
            payload["minutes_before"] = abs(alarm.relativeOffset) / 60.0
        }
        return payload
    }
}

func serializeRecurrenceRules(_ rules: [EKRecurrenceRule]?) -> [[String: Any]] {
    (rules ?? []).map { rule in
        var payload: [String: Any] = [
            "frequency": recurrenceFrequencyName(rule.frequency),
            "interval": rule.interval,
            "first_day_of_week": rule.firstDayOfTheWeek,
        ]
        if let days = rule.daysOfTheWeek, !days.isEmpty {
            payload["days_of_week"] = days.map(serializeRecurrenceDay)
        }
        if let days = rule.daysOfTheMonth, !days.isEmpty {
            payload["days_of_month"] = days.map { $0.intValue }
        }
        if let days = rule.daysOfTheYear, !days.isEmpty {
            payload["days_of_year"] = days.map { $0.intValue }
        }
        if let weeks = rule.weeksOfTheYear, !weeks.isEmpty {
            payload["weeks_of_year"] = weeks.map { $0.intValue }
        }
        if let months = rule.monthsOfTheYear, !months.isEmpty {
            payload["months_of_year"] = months.map { $0.intValue }
        }
        if let positions = rule.setPositions, !positions.isEmpty {
            payload["set_positions"] = positions.map { $0.intValue }
        }
        if let end = rule.recurrenceEnd {
            payload["end_date"] = formatDate(end.endDate)
            if end.occurrenceCount > 0 {
                payload["occurrence_count"] = end.occurrenceCount
            }
        }
        return compact(payload)
    }
}

func serializeRecurrenceDay(_ day: EKRecurrenceDayOfWeek) -> [String: Any] {
    compact([
        "day": weekdayName(day.dayOfTheWeek),
        "day_number": day.dayOfTheWeek.rawValue,
        "week_number": day.weekNumber,
    ])
}

func recurrenceFrequencyName(_ frequency: EKRecurrenceFrequency) -> String {
    switch frequency {
    case .daily:
        return "daily"
    case .weekly:
        return "weekly"
    case .monthly:
        return "monthly"
    case .yearly:
        return "yearly"
    @unknown default:
        return String(frequency.rawValue)
    }
}

func serializeParticipant(_ participant: EKParticipant) -> [String: Any] {
    return compact([
        "name": participant.name ?? "",
        "url": participant.url.absoluteString,
        "status": participant.participantStatus.rawValue,
        "role": participant.participantRole.rawValue,
        "type": participant.participantType.rawValue,
    ])
}

func replaceUrgentAlarm(_ reminder: EKReminder, urgent: Bool) throws {
    let dueDate = reminderDueDate(reminder)
    for alarm in reminder.alarms ?? [] where isUrgentAlarm(alarm) || isDueDateAlarm(alarm, dueDate: dueDate) {
        reminder.removeAlarm(alarm)
    }
    guard urgent else {
        return
    }
    guard let dueDate = reminderDueDate(reminder) else {
        throw HelperError("urgent reminders require due", code: "invalid_urgent")
    }
    let alarm = EKAlarm(absoluteDate: dueDate)
    alarm.soundName = urgentAlarmSoundName
    reminder.addAlarm(alarm)
}

func removeUrgentAlarms(_ reminder: EKReminder) {
    let dueDate = reminderDueDate(reminder)
    for alarm in reminder.alarms ?? [] where isUrgentAlarm(alarm) || isDueDateAlarm(alarm, dueDate: dueDate) {
        reminder.removeAlarm(alarm)
    }
}

func reminderDueDate(_ reminder: EKReminder) -> Date? {
    guard let components = reminder.dueDateComponents else {
        return nil
    }
    return components.calendar?.date(from: components) ?? Calendar.current.date(from: components)
}

func hasUrgentAlarm(_ item: EKCalendarItem) -> Bool {
    if let reminder = item as? EKReminder {
        let dueDate = reminderDueDate(reminder)
        return (reminder.alarms ?? []).contains { alarm in
            isUrgentAlarm(alarm) || isDueDateAlarm(alarm, dueDate: dueDate)
        }
    }
    return (item.alarms ?? []).contains(where: isUrgentAlarm)
}

func isUrgentAlarm(_ alarm: EKAlarm) -> Bool {
    !(alarm.soundName ?? "").isEmpty
}

func isDueDateAlarm(_ alarm: EKAlarm, dueDate: Date?) -> Bool {
    guard let dueDate = dueDate, let alarmDate = alarm.absoluteDate else {
        return false
    }
    return abs(alarmDate.timeIntervalSince(dueDate)) < 1.0
}

func setTravelTime(_ event: EKEvent, fields: [String: Any]) throws {
    guard runtimeSupports(EKEvent.self, selectorName: "setTravelTime:") else {
        throw HelperError("Calendar travel time is not available on this EventKit runtime", code: "unsupported_private_feature")
    }
    let seconds = doubleValue(fields["travel_time_minutes"]).map { $0 * 60.0 } ?? 0
    callDoubleSetter(event, selectorName: "setTravelTime:", value: max(0, seconds))
}

func getTravelTime(_ event: EKEvent) -> Double? {
    getDouble(event, selectorName: "travelTime")
}

func allowsTravelTimeModifications(_ event: EKEvent) -> Bool {
    getBool(event, selectorName: "allowsTravelTimeModifications") ?? false
}

func replaceAttachments(_ item: EKCalendarItem, attachments: [[String: Any]]) throws {
    if item is EKReminder {
        throw HelperError(
            "Reminders attachments are handled by the private ReminderKit attachment backend, not EventKit attachments",
            code: "unsupported_private_feature"
        )
    }
    guard runtimeSupports(EKCalendarItem.self, selectorName: "setAttachments:"),
          let attachmentClass = NSClassFromString("EKAttachment") as? NSObject.Type else {
        throw HelperError("Apple calendar item attachments are not available on this EventKit runtime", code: "unsupported_private_feature")
    }
    let objects = try attachments.map { spec -> NSObject in
        let path = string(spec["file_path"] ?? spec["path"] ?? spec["local_path"])
        if path.isEmpty {
            throw HelperError("attachment file_path is required", code: "invalid_attachment")
        }
        guard FileManager.default.fileExists(atPath: NSString(string: path).expandingTildeInPath) else {
            throw HelperError("attachment file does not exist: \(path)", code: "invalid_attachment")
        }
        guard let attachment = makeAttachment(attachmentClass: attachmentClass, filePath: path) else {
            throw HelperError("Could not create Apple attachment for \(path)", code: "private_attachment_failed")
        }
        return attachment
    }
    let object = item as NSObject
    guard object.responds(to: NSSelectorFromString("setAttachments:")) else {
        throw HelperError("Apple calendar item attachments are not mutable", code: "unsupported_private_feature")
    }
    _ = object.perform(NSSelectorFromString("setAttachments:"), with: objects as NSArray)
}

func makeAttachment(attachmentClass: NSObject.Type, filePath: String) -> NSObject? {
    guard let object = attachmentClass.perform(NSSelectorFromString("alloc"))?.takeUnretainedValue() as? NSObject else {
        return nil
    }
    let url = NSURL(fileURLWithPath: NSString(string: filePath).expandingTildeInPath)
    return object.perform(
        NSSelectorFromString("initWithFilepath:"),
        with: url
    )?.takeUnretainedValue() as? NSObject
}

func serializeAttachments(_ item: EKCalendarItem) -> [[String: Any]] {
    let object = item as NSObject
    guard object.responds(to: NSSelectorFromString("attachments")),
          let raw = object.perform(NSSelectorFromString("attachments"))?.takeUnretainedValue() as? NSArray else {
        return []
    }
    return raw.compactMap { value in
        guard let attachment = value as? NSObject else {
            return nil
        }
        return compact([
            "file_name": stringFromSelector(attachment, "fileName"),
            "file_size": stringFromSelector(attachment, "fileSize"),
            "file_format": stringFromSelector(attachment, "fileFormat"),
            "url": urlStringFromSelector(attachment, "URL"),
            "url_string": stringFromSelector(attachment, "URLString"),
            "local_url": urlStringFromSelector(attachment, "localURL"),
        ])
    }
}

func serializePrivateReminderAttachments(_ reminder: EKReminder) -> [[String: Any]] {
    guard PrivateReminderKitBridge.supportsAttachments,
          let bridge = PrivateReminderKitBridge(),
          let attachments = try? bridge.attachments(candidateIDs: reminderIdentifierCandidates(reminder)) else {
        return []
    }
    return attachments
}

func fetchReminders(completed: Bool, calendars: [EKCalendar]) throws -> [EKReminder] {
    let predicate: NSPredicate
    if completed {
        predicate = store.predicateForCompletedReminders(withCompletionDateStarting: nil, ending: nil, calendars: calendars)
    } else {
        predicate = store.predicateForIncompleteReminders(withDueDateStarting: nil, ending: nil, calendars: calendars)
    }
    let semaphore = DispatchSemaphore(value: 0)
    var reminders: [EKReminder] = []
    store.fetchReminders(matching: predicate) { fetched in
        reminders = fetched ?? []
        semaphore.signal()
    }
    semaphore.wait()
    return reminders
}

func privateReminderBridge() throws -> PrivateReminderKitBridge {
    guard let bridge = PrivateReminderKitBridge() else {
        throw HelperError(
            "Apple private ReminderKit is unavailable on this macOS runtime",
            code: "unsupported_private_feature",
            remediation: "Use macOS Reminders with /System/Library/PrivateFrameworks/ReminderKit.framework available."
        )
    }
    return bridge
}

func privateReminderFieldsRequested(_ fields: [String: Any]) -> Bool {
    if fields.keys.contains("section_id") || fields.keys.contains("parent_id") {
        return true
    }
    if fields["flagged"] != nil || fields["urgent"] != nil {
        return true
    }
    if fields.keys.contains("tags") || fields.keys.contains("subtasks") || fields.keys.contains("attachments") || fields.keys.contains("alarms") || fields.keys.contains("recurrence") {
        return true
    }
    return false
}

func requirePrivateReminderSupport(_ fields: [String: Any]) throws {
    if privateReminderFieldsRequested(fields), !PrivateReminderKitBridge.isAvailable {
        throw HelperError(
            "sections, tags, subtasks, attachments, and flagged state require the private Reminders backend",
            code: "unsupported_private_feature"
        )
    }
    if fields.keys.contains("attachments"), !PrivateReminderKitBridge.supportsAttachments {
        throw HelperError(
            "Reminders attachments require the private ReminderKit attachment backend",
            code: "unsupported_private_feature"
        )
    }
}

func listIDMap(_ calendars: [EKCalendar]) -> [String: String] {
    var result: [String: String] = [:]
    for calendar in calendars {
        result[calendar.title] = calendar.calendarIdentifier
    }
    return result
}

func reminderIdentifierCandidates(_ reminder: EKReminder) -> [String] {
    var candidates = [reminder.calendarItemIdentifier]
    if let external = reminder.calendarItemExternalIdentifier {
        candidates.append(external)
    }
    return uniqueStrings(candidates)
}

func reminderEventKitIDMap(_ payloads: [[String: Any]], snapshot: SQLiteReminderSnapshot) -> [String: String] {
    var result: [String: String] = [:]
    for payload in payloads {
        let eventKitID = string(payload["id"])
        guard !eventKitID.isEmpty else {
            continue
        }
        if let metadata = snapshot.metadata(forIdentifiers: [
            eventKitID,
            string(payload["external_identifier"]),
        ]) {
            result[normalizedIdentifier(metadata.privateID)] = eventKitID
        }
    }
    return result
}

func enrichReminderPayload(
    _ payload: [String: Any],
    snapshot: SQLiteReminderSnapshot,
    eventKitIDByReminderKitID: [String: String]
) -> [String: Any] {
    guard let metadata = snapshot.metadata(forIdentifiers: [
        string(payload["id"]),
        string(payload["external_identifier"]),
    ]) else {
        return payload
    }
    var enriched = payload
    if !metadata.privateID.isEmpty {
        enriched["reminderkit_id"] = metadata.privateID
    }
    if !metadata.sectionID.isEmpty {
        enriched["section_id"] = metadata.sectionID
    }
    if !metadata.parentID.isEmpty {
        enriched["parent_id"] = eventKitIDByReminderKitID[normalizedIdentifier(metadata.parentID)] ?? metadata.parentID
    }
    if !metadata.tags.isEmpty {
        enriched["tags"] = metadata.tags
    }
    enriched["flagged"] = metadata.flagged
    enriched["urgent"] = metadata.urgent
    if !metadata.alarms.isEmpty {
        enriched["alarms"] = metadata.alarms
    }
    return compact(enriched)
}

func enrichReminderPayloadWithSubtasks(
    _ payload: [String: Any],
    candidates: [EKReminder],
    snapshot: SQLiteReminderSnapshot,
    eventKitIDByReminderKitID: [String: String]
) -> [String: Any] {
    var enriched = enrichReminderPayload(
        payload,
        snapshot: snapshot,
        eventKitIDByReminderKitID: eventKitIDByReminderKitID
    )
    let childIDs = snapshot.childIDs(forParentID: string(enriched["reminderkit_id"]))
        + snapshot.childIDs(forParentID: string(enriched["id"]))
        + snapshot.childIDs(forParentID: string(enriched["external_identifier"]))
    var subtasks: [[String: Any]] = []
    for childID in uniqueStrings(childIDs) {
        if let child = reminderByAnyIdentifier(childID, candidates: candidates, snapshot: snapshot) {
            var childPayload = enrichReminderPayload(
                serializeReminder(child, detail: true),
                snapshot: snapshot,
                eventKitIDByReminderKitID: eventKitIDByReminderKitID
            )
            childPayload.removeValue(forKey: "subtasks")
            subtasks.append(childPayload)
        } else {
            subtasks.append(compact([
                "id": eventKitIDByReminderKitID[normalizedIdentifier(childID)] ?? childID,
                "reminderkit_id": childID,
                "type": "reminder",
            ]))
        }
    }
    if !subtasks.isEmpty {
        enriched["subtasks"] = subtasks
    }
    return compact(enriched)
}

func reminderByAnyIdentifier(
    _ id: String,
    candidates: [EKReminder],
    snapshot: SQLiteReminderSnapshot
) -> EKReminder? {
    let targetKeys = Set(identifierKeys(id))
    if targetKeys.isEmpty {
        return nil
    }
    for reminder in candidates {
        let directKeys = Set(identifierKeys(reminder.calendarItemIdentifier) + identifierKeys(reminder.calendarItemExternalIdentifier ?? ""))
        if !targetKeys.isDisjoint(with: directKeys) {
            return reminder
        }
        if let metadata = snapshot.metadata(forIdentifiers: reminderIdentifierCandidates(reminder)) {
            let metadataKeys = Set(identifierKeys(metadata.privateID))
            if !targetKeys.isDisjoint(with: metadataKeys) {
                return reminder
            }
        }
    }
    return nil
}

func reminderMatchesDue(_ payload: [String: Any], start: Date?, end: Date?) -> Bool {
    if start == nil && end == nil {
        return true
    }
    guard let due = parseDate(payload["due"]) else {
        return false
    }
    if let start = start, due < start {
        return false
    }
    if let end = end, due > end {
        return false
    }
    return true
}

func reminderIsOverdue(_ payload: [String: Any], now: Date) -> Bool {
    let dueText = string(payload["due"])
    guard let due = parseDate(dueText) else {
        return false
    }
    if isDateOnlyText(dueText) {
        let calendar = Calendar.current
        return calendar.startOfDay(for: due) < calendar.startOfDay(for: now)
    }
    return due < now
}

func reminderMatchesPriority(_ payload: [String: Any], priorityFilter: Int?) -> Bool {
    guard let filter = priorityFilter else {
        return true
    }
    let priority = int64(payload["priority"])
    return priority == Int64(max(0, min(filter, 9)))
}

func reminderMatchesText(_ reminder: EKReminder, text: String) -> Bool {
    if text.isEmpty {
        return true
    }
    return [
        reminder.title ?? "",
        reminder.notes ?? "",
        reminder.url?.absoluteString ?? "",
    ].contains { value in value.lowercased().contains(text) }
}

func eventMatchesText(_ event: EKEvent, text: String) -> Bool {
    if text.isEmpty {
        return true
    }
    return [
        event.title ?? "",
        event.notes ?? "",
        event.url?.absoluteString ?? "",
    ].contains { value in value.lowercased().contains(text) }
}

func calendarContainsItems(_ calendar: EKCalendar) -> Bool {
    if store.calendars(for: .reminder).contains(where: { $0.calendarIdentifier == calendar.calendarIdentifier }) {
        let incomplete = (try? fetchReminders(completed: false, calendars: [calendar])) ?? []
        if !incomplete.isEmpty {
            return true
        }
        let completed = (try? fetchReminders(completed: true, calendars: [calendar])) ?? []
        return !completed.isEmpty
    }
    let now = Date()
    let start = Calendar.current.date(byAdding: .year, value: -5, to: now) ?? now
    let end = Calendar.current.date(byAdding: .year, value: 5, to: now) ?? now
    let predicate = store.predicateForEvents(withStart: start, end: end, calendars: [calendar])
    return !store.events(matching: predicate).isEmpty
}

struct SQLiteReminderMetadata {
    let privateID: String
    let listID: String
    let sectionID: String
    let parentID: String
    let tags: [String]
    let flagged: Bool
    let urgent: Bool
    let alarms: [[String: Any]]
}

struct SQLiteReminderSnapshot {
    let sections: [[String: Any]]
    let metadataByKey: [String: SQLiteReminderMetadata]
    let sectionByKey: [String: [String: Any]]
    let childCountByKey: [String: Int]
    let childIDsByParentKey: [String: [String]]
    let sectionMemberCountByKey: [String: Int]

    static let empty = SQLiteReminderSnapshot(
        sections: [],
        metadataByKey: [:],
        sectionByKey: [:],
        childCountByKey: [:],
        childIDsByParentKey: [:],
        sectionMemberCountByKey: [:]
    )

    static func load(calendarListIDsByTitle: [String: String]) -> SQLiteReminderSnapshot {
        guard let path = findDatabasePath(), let db = try? SQLiteDatabase(path: path) else {
            return .empty
        }
        guard db.tableExists("ZREMCDREMINDER") else {
            return .empty
        }

        let listNameColumn = db.columnExists("ZREMCDBASELIST", column: "ZNAME") ? "ZNAME" : "ZDISPLAYNAME"
        let listCKExpr = db.columnExists("ZREMCDBASELIST", column: "ZCKIDENTIFIER") ? "ZCKIDENTIFIER" : "''"
        let listRows = (try? db.query(
            "SELECT Z_PK, \(listNameColumn) AS ZNAME, \(listCKExpr) AS ZCKIDENTIFIER FROM ZREMCDBASELIST WHERE ZMARKEDFORDELETION = 0"
        )) ?? []
        var listTitleByPK: [Int64: String] = [:]
        var listIDByPK: [Int64: String] = [:]
        for row in listRows {
            let pk = int64(row["Z_PK"])
            let title = string(row["ZNAME"])
            let ck = string(row["ZCKIDENTIFIER"])
            listTitleByPK[pk] = title
            listIDByPK[pk] = calendarListIDsByTitle[title] ?? ck
        }

        var sections: [[String: Any]] = []
        var sectionByPrivateID: [String: [String: Any]] = [:]
        if db.tableExists("ZREMCDBASESECTION") {
            let sectionRows = (try? db.query(
                "SELECT Z_PK, ZDISPLAYNAME, ZLIST, ZCKIDENTIFIER FROM ZREMCDBASESECTION WHERE ZMARKEDFORDELETION = 0 ORDER BY ZLIST, Z_PK"
            )) ?? []
            for row in sectionRows {
                let listPK = int64(row["ZLIST"])
                let sectionID = string(row["ZCKIDENTIFIER"]).isEmpty ? String(int64(row["Z_PK"])) : string(row["ZCKIDENTIFIER"])
                let section = compact([
                    "id": sectionID,
                    "type": "section",
                    "title": string(row["ZDISPLAYNAME"]),
                    "list_id": listIDByPK[listPK] ?? "",
                    "list_title": listTitleByPK[listPK] ?? "",
                ])
                sections.append(section)
                for key in identifierKeys(sectionID) {
                    sectionByPrivateID[key] = section
                }
            }
        }

        let memberships = db.columnExists("ZREMCDBASELIST", column: "ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA")
            ? loadSectionMemberships(db: db)
            : [:]

        let flaggedExpr = db.columnExists("ZREMCDREMINDER", column: "ZFLAGGED") ? "ZFLAGGED" : "0"
        let urgentExpr = db.columnExists("ZREMCDREMINDER", column: "ZISURGENTSTATEENABLEDFORCURRENTUSER") ? "ZISURGENTSTATEENABLEDFORCURRENTUSER" : "0"
        let reminderRows = (try? db.query(
            "SELECT Z_PK, ZCKIDENTIFIER, ZLIST, ZPARENTREMINDER, \(flaggedExpr) AS ZFLAGGED, \(urgentExpr) AS ZURGENT FROM ZREMCDREMINDER WHERE ZMARKEDFORDELETION = 0 AND ZACCOUNT IS NOT NULL"
        )) ?? []

        var privateIDByPK: [Int64: String] = [:]
        var listPKByPK: [Int64: Int64] = [:]
        var parentPKByPK: [Int64: Int64] = [:]
        var flaggedByPK: [Int64: Bool] = [:]
        var urgentByPK: [Int64: Bool] = [:]
        for row in reminderRows {
            let pk = int64(row["Z_PK"])
            privateIDByPK[pk] = string(row["ZCKIDENTIFIER"])
            listPKByPK[pk] = int64(row["ZLIST"])
            parentPKByPK[pk] = int64(row["ZPARENTREMINDER"])
            flaggedByPK[pk] = int64(row["ZFLAGGED"]) != 0
            urgentByPK[pk] = int64(row["ZURGENT"]) != 0
        }

        let tagsByPK = loadTags(db: db)
        let dueDateDeltaAlarmsByPrivateID = loadDueDateDeltaAlarms(db: db)
        var childCounts: [String: Int] = [:]
        var childIDsByParentKey: [String: [String]] = [:]
        for parentPK in parentPKByPK.values where parentPK > 0 {
            let parentID = privateIDByPK[parentPK] ?? String(parentPK)
            for key in identifierKeys(parentID) {
                childCounts[key, default: 0] += 1
            }
        }

        var sectionMemberCounts: [String: Int] = [:]
        var metadataByKey: [String: SQLiteReminderMetadata] = [:]
        for row in reminderRows {
            let pk = int64(row["Z_PK"])
            let privateID = privateIDByPK[pk] ?? ""
            let listPK = listPKByPK[pk] ?? 0
            let parentPK = parentPKByPK[pk] ?? 0
            let parentID = parentPK > 0 ? (privateIDByPK[parentPK] ?? String(parentPK)) : ""
            let sectionID = memberships[privateID] ?? ""
            if !parentID.isEmpty {
                for key in identifierKeys(parentID) {
                    childIDsByParentKey[key, default: []].append(privateID)
                }
            }
            if !sectionID.isEmpty {
                for key in identifierKeys(sectionID) {
                    sectionMemberCounts[key, default: 0] += 1
                }
            }
            let metadata = SQLiteReminderMetadata(
                privateID: privateID,
                listID: listIDByPK[listPK] ?? "",
                sectionID: sectionID,
                parentID: parentID,
                tags: tagsByPK[pk] ?? [],
                flagged: flaggedByPK[pk] ?? false,
                urgent: urgentByPK[pk] ?? false,
                alarms: dueDateDeltaAlarmsByPrivateID[privateID] ?? []
            )
            for key in identifierKeys(privateID) + identifierKeys(String(pk)) {
                metadataByKey[key] = metadata
            }
        }

        return SQLiteReminderSnapshot(
            sections: sections,
            metadataByKey: metadataByKey,
            sectionByKey: sectionByPrivateID,
            childCountByKey: childCounts,
            childIDsByParentKey: childIDsByParentKey,
            sectionMemberCountByKey: sectionMemberCounts
        )
    }

    static func findDatabasePath() -> String? {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let stores = home
            .appendingPathComponent("Library")
            .appendingPathComponent("Group Containers")
            .appendingPathComponent("group.com.apple.reminders")
            .appendingPathComponent("Container_v1")
            .appendingPathComponent("Stores")
        guard let enumerator = FileManager.default.enumerator(at: stores, includingPropertiesForKeys: nil) else {
            return nil
        }
        var candidates: [URL] = []
        for case let url as URL in enumerator {
            if url.pathExtension == "sqlite" {
                candidates.append(url)
            }
        }
        candidates.sort { lhs, rhs in
            let leftDate = ((try? lhs.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate) ?? .distantPast
            let rightDate = ((try? rhs.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate) ?? .distantPast
            return leftDate > rightDate
        }
        for candidate in candidates {
            if let db = try? SQLiteDatabase(path: candidate.path), db.tableExists("ZREMCDREMINDER") {
                return candidate.path
            }
        }
        return nil
    }

    func metadata(forIdentifiers identifiers: [String]) -> SQLiteReminderMetadata? {
        for identifier in identifiers {
            for key in identifierKeys(identifier) {
                if let metadata = metadataByKey[key] {
                    return metadata
                }
            }
        }
        return nil
    }

    func section(for id: String) -> [String: Any]? {
        for key in identifierKeys(id) {
            if let section = sectionByKey[key] {
                return section
            }
        }
        return nil
    }

    func reminderHasSubtasks(_ id: String) -> Bool {
        for key in identifierKeys(id) {
            if (childCountByKey[key] ?? 0) > 0 {
                return true
            }
            if let metadata = metadataByKey[key] {
                for privateKey in identifierKeys(metadata.privateID) where (childCountByKey[privateKey] ?? 0) > 0 {
                    return true
                }
            }
        }
        return false
    }

    func sectionHasReminders(_ id: String) -> Bool {
        for key in identifierKeys(id) where (sectionMemberCountByKey[key] ?? 0) > 0 {
            return true
        }
        return false
    }

    func childIDs(forParentID id: String) -> [String] {
        var result: [String] = []
        for key in identifierKeys(id) {
            result.append(contentsOf: childIDsByParentKey[key] ?? [])
        }
        return uniqueStrings(result)
    }
}

func loadSectionMemberships(db: SQLiteDatabase) -> [String: String] {
    let rows = (try? db.query(
        "SELECT ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA FROM ZREMCDBASELIST WHERE ZMARKEDFORDELETION = 0"
    )) ?? []
    var result: [String: String] = [:]
    for row in rows {
        guard let text = rowText(row["ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA"]),
              let data = text.data(using: .utf8),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let memberships = root["memberships"] as? [[String: Any]] else {
            continue
        }
        for membership in memberships {
            if bool(membership["isObsolete"], defaultValue: false) {
                continue
            }
            let memberID = string(membership["memberID"])
            let groupID = string(membership["groupID"])
            if !memberID.isEmpty && !groupID.isEmpty {
                result[memberID] = groupID
            }
        }
    }
    return result
}

func loadTags(db: SQLiteDatabase) -> [Int64: [String]] {
    guard db.tableExists("ZREMCDOBJECT"), db.tableExists("ZREMCDHASHTAGLABEL") else {
        return [:]
    }
    let reminderColumn = db.columnExists("ZREMCDOBJECT", column: "ZREMINDER3") ? "ZREMINDER3" : "ZREMINDER"
    let tagColumn = db.columnExists("ZREMCDOBJECT", column: "ZHASHTAGLABEL") ? "ZHASHTAGLABEL" : "ZHASHTAGLABEL3"
    guard db.columnExists("ZREMCDOBJECT", column: reminderColumn),
          db.columnExists("ZREMCDOBJECT", column: tagColumn) else {
        return [:]
    }
    let rows = (try? db.query(
        "SELECT o.\(reminderColumn) AS reminder_pk, h.ZNAME AS tag_name FROM ZREMCDOBJECT o JOIN ZREMCDHASHTAGLABEL h ON o.\(tagColumn) = h.Z_PK WHERE o.\(reminderColumn) IS NOT NULL"
    )) ?? []
    var result: [Int64: [String]] = [:]
    for row in rows {
        let pk = int64(row["reminder_pk"])
        let tag = string(row["tag_name"])
        if pk > 0 && !tag.isEmpty {
            result[pk, default: []].append(tag)
        }
    }
    return result
}

func loadDueDateDeltaAlarms(db: SQLiteDatabase) -> [String: [[String: Any]]] {
    guard db.tableExists("ZREMCDDUEDATEDELTAALERT"),
          db.columnExists("ZREMCDDUEDATEDELTAALERT", column: "ZREMINDERIDENTIFIER") else {
        return [:]
    }
    let identifierExpr = db.columnExists("ZREMCDDUEDATEDELTAALERT", column: "ZIDENTIFIER") ? "ZIDENTIFIER" : "NULL"
    let rows = (try? db.query(
        "SELECT ZREMINDERIDENTIFIER, \(identifierExpr) AS ZIDENTIFIER, ZDUEDATEDELTAUNIT, ZDUEDATEDELTACOUNT FROM ZREMCDDUEDATEDELTAALERT ORDER BY ZSORTORDER, Z_PK"
    )) ?? []
    var result: [String: [[String: Any]]] = [:]
    for row in rows {
        guard let reminderID = uuidString(from: row["ZREMINDERIDENTIFIER"]) else {
            continue
        }
        let unit = int64(row["ZDUEDATEDELTAUNIT"])
        let count = int64(row["ZDUEDATEDELTACOUNT"])
        var payload = dueDateDeltaAlarmPayload(unit: unit, count: count)
        if let identifier = uuidString(from: row["ZIDENTIFIER"]) {
            payload["id"] = identifier
        }
        result[reminderID, default: []].append(payload)
    }
    return result
}

func dueDateDeltaAlertIdentifiers(candidateIDs: [String]) -> [String] {
    guard let path = SQLiteReminderSnapshot.findDatabasePath(),
          let db = try? SQLiteDatabase(path: path) else {
        return []
    }
    let targetKeys = Set(candidateIDs.flatMap(identifierKeys))
    var result: [String] = []
    for (reminderID, alarms) in loadDueDateDeltaAlarms(db: db) {
        if targetKeys.isDisjoint(with: Set(identifierKeys(reminderID))) {
            continue
        }
        for alarm in alarms {
            let identifier = string(alarm["id"])
            if !identifier.isEmpty {
                result.append(identifier)
            }
        }
    }
    return uniqueStrings(result)
}

func dueDateDeltaAlarmPayload(unit: Int64, count: Int64) -> [String: Any] {
    let seconds: Int64
    switch unit {
    case 0:
        seconds = count * 60
    case 1:
        seconds = count * 3600
    case 2:
        seconds = count * 86400
    case 3:
        seconds = count * 7 * 86400
    case 4:
        seconds = count * 30 * 86400
    default:
        seconds = count * 60
    }
    var payload: [String: Any] = [
        "type": "before_due",
    ]
    if seconds < 0, seconds % 60 == 0 {
        payload["minutes_before"] = abs(seconds / 60)
    }
    return payload
}

func uuidString(from value: Any?) -> String? {
    guard let data = value as? Data, data.count == 16 else {
        return nil
    }
    return data.withUnsafeBytes { raw -> String? in
        guard let base = raw.bindMemory(to: UInt8.self).baseAddress else {
            return nil
        }
        return NSUUID(uuidBytes: base).uuidString
    }
}

final class SQLiteDatabase {
    let handle: OpaquePointer

    init(path: String) throws {
        var raw: OpaquePointer?
        let flags = SQLITE_OPEN_READONLY | SQLITE_OPEN_NOMUTEX
        if sqlite3_open_v2(path, &raw, flags, nil) != SQLITE_OK || raw == nil {
            throw HelperError("Could not open Reminders SQLite database", code: "sqlite_open_failed")
        }
        handle = raw!
    }

    deinit {
        sqlite3_close(handle)
    }

    func tableExists(_ table: String) -> Bool {
        let escaped = table.replacingOccurrences(of: "'", with: "''")
        let rows = (try? query("SELECT name FROM sqlite_master WHERE type='table' AND name='\(escaped)' LIMIT 1")) ?? []
        return !rows.isEmpty
    }

    func columnExists(_ table: String, column: String) -> Bool {
        let rows = (try? query("PRAGMA table_info(\(table))")) ?? []
        return rows.contains { string($0["name"]).caseInsensitiveCompare(column) == .orderedSame }
    }

    func query(_ sql: String) throws -> [[String: Any]] {
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(handle, sql, -1, &statement, nil) == SQLITE_OK, let statement = statement else {
            throw HelperError("SQLite query failed: \(String(cString: sqlite3_errmsg(handle)))", code: "sqlite_query_failed")
        }
        defer { sqlite3_finalize(statement) }
        var rows: [[String: Any]] = []
        while sqlite3_step(statement) == SQLITE_ROW {
            var row: [String: Any] = [:]
            for index in 0..<sqlite3_column_count(statement) {
                let name = String(cString: sqlite3_column_name(statement, index))
                switch sqlite3_column_type(statement, index) {
                case SQLITE_INTEGER:
                    row[name] = sqlite3_column_int64(statement, index)
                case SQLITE_FLOAT:
                    row[name] = sqlite3_column_double(statement, index)
                case SQLITE_TEXT:
                    row[name] = String(cString: sqlite3_column_text(statement, index))
                case SQLITE_BLOB:
                    if let bytes = sqlite3_column_blob(statement, index) {
                        row[name] = Data(bytes: bytes, count: Int(sqlite3_column_bytes(statement, index)))
                    }
                default:
                    break
                }
            }
            rows.append(row)
        }
        return rows
    }
}

final class PrivateReminderKitBridge {
    let remStore: NSObject
    let accountsView: NSObject?
    let listsView: NSObject?
    let sectionsView: NSObject?

    static var isAvailable: Bool {
        guard let bundle = Bundle(path: "/System/Library/PrivateFrameworks/ReminderKit.framework") else {
            return false
        }
        _ = bundle.load()
        return NSClassFromString("REMStore") != nil
            && NSClassFromString("REMSaveRequest") != nil
            && NSClassFromString("REMObjectID") != nil
    }

    static var supportsAttachments: Bool {
        guard isAvailable,
              let reminderClass = NSClassFromString("REMReminder"),
              let contextClass = NSClassFromString("REMReminderAttachmentContextChangeItem") else {
            return false
        }
        return runtimeSupports(reminderClass, selectorName: "attachmentContext")
            && runtimeSupports(contextClass, selectorName: "attachments")
            && runtimeSupports(contextClass, selectorName: "removeAllAttachments")
            && runtimeSupports(contextClass, selectorName: "addImageAttachmentWithURL:width:height:error:")
            && runtimeSupports(contextClass, selectorName: "addURLAttachmentWithURL:")
    }

    init?() {
        guard Self.isAvailable,
              let storeClass = NSClassFromString("REMStore") as? NSObject.Type else {
            return nil
        }
        remStore = storeClass.init()
        accountsView = Self.dataView(className: "REMAccountsDataView", store: remStore)
        listsView = Self.dataView(className: "REMListsDataView", store: remStore)
        sectionsView = Self.dataView(className: "REMListSectionsDataView", store: remStore)
    }

    static func dataView(className: String, store: NSObject) -> NSObject? {
        guard let cls = NSClassFromString(className) as? NSObject.Type else {
            return nil
        }
        let object = cls.perform(NSSelectorFromString("alloc"))?.takeUnretainedValue() as? NSObject
        _ = object?.perform(NSSelectorFromString("initWithStore:"), with: store)
        return object
    }

    func createSection(item: [String: Any], eventStore: EKEventStore) throws -> [String: Any] {
        let title = try requiredTitle(item)
        let listID = string(item["list_id"])
        let listTitle = string(item["list_title"])
        guard !listID.isEmpty || !listTitle.isEmpty else {
            throw HelperError("list_id is required to create a section", code: "invalid_section")
        }
        let calendar = listID.isEmpty ? nil : eventStore.calendar(withIdentifier: listID)
        let list = try findList(calendar: calendar, title: listTitle)
        let save = try saveRequest()
        guard let listChange = save.perform(NSSelectorFromString("updateList:"), with: list)?.takeUnretainedValue() as? NSObject,
              let sectionContext = listChange.perform(NSSelectorFromString("sectionsContextChangeItem"))?.takeUnretainedValue() as? NSObject,
              let sectionChange = save.perform(
                  NSSelectorFromString("addListSectionWithDisplayName:toListSectionContextChangeItem:"),
                  with: title as NSString,
                  with: sectionContext
              )?.takeUnretainedValue() as? NSObject else {
            throw HelperError("Could not create Reminders section", code: "private_section_create_failed")
        }
        try saveSynchronously(save)
        let info = objectIDInfo(remObjectID(sectionChange))
        return compact([
            "id": info.id,
            "type": "section",
            "title": title,
            "list_id": calendar?.calendarIdentifier ?? listID,
            "list_title": calendar?.title ?? listTitle,
            "url": info.url,
        ])
    }

    func updateSection(id: String, fields: [String: Any]) throws -> [String: Any]? {
        guard let section = fetchSection(id: id) else {
            return nil
        }
        let title = string(fields["title"])
        if title.isEmpty {
            return sectionPayload(section)
        }
        let save = try saveRequest()
        guard let change = save.perform(NSSelectorFromString("updateListSection:"), with: section)?.takeUnretainedValue() as? NSObject else {
            throw HelperError("Could not update Reminders section", code: "private_section_update_failed")
        }
        if !callStringSetter(change, selectors: ["setDisplayName:", "setName:"], value: title) {
            throw HelperError("This macOS ReminderKit build does not expose section rename", code: "unsupported_private_feature")
        }
        try saveSynchronously(save)
        var payload = sectionPayload(section)
        payload["title"] = title
        return payload
    }

    func deleteSection(id: String) throws {
        guard let section = fetchSection(id: id) else {
            throw HelperError("Reminders section not found: \(id)", code: "not_found")
        }
        let save = try saveRequest()
        guard let change = save.perform(NSSelectorFromString("updateListSection:"), with: section)?.takeUnretainedValue() as? NSObject else {
            throw HelperError("Could not delete Reminders section", code: "private_section_delete_failed")
        }
        _ = change.perform(NSSelectorFromString("removeFromList"))
        try saveSynchronously(save)
    }

    func createSubtask(item: [String: Any]) throws -> [String: Any] {
        let parentID = string(item["parent_id"])
        guard !parentID.isEmpty else {
            throw HelperError("parent_id is required for subtask creation", code: "invalid_subtask")
        }
        let parent = try fetchReminder(candidateIDs: [parentID])
        let title = try requiredTitle(item)
        let save = try saveRequest()
        guard let change = save.perform(NSSelectorFromString("updateReminder:"), with: parent)?.takeUnretainedValue() as? NSObject,
              let subtaskContext = change.perform(NSSelectorFromString("subtaskContext"))?.takeUnretainedValue() as? NSObject,
              let subtask = save.perform(
                  NSSelectorFromString("addReminderWithTitle:toReminderSubtaskContextChangeItem:"),
                  with: title as NSString,
                  with: subtaskContext
              )?.takeUnretainedValue() as? NSObject else {
            throw HelperError("Could not create Reminders subtask", code: "private_subtask_create_failed")
        }
        try saveSynchronously(save)
        let info = objectIDInfo(remObjectID(subtask))
        if privateReminderFieldsRequested(item) {
            try patchReminder(candidateIDs: [info.id, info.url], fields: item.filteringKeys { $0 != "parent_id" && $0 != "subtasks" })
        }
        return compact([
            "id": info.id,
            "type": "reminder",
            "title": title,
            "parent_id": parentID,
            "url": info.url,
        ])
    }

    func patchReminder(candidateIDs: [String], fields: [String: Any]) throws {
        guard privateReminderFieldsRequested(fields) else {
            return
        }
        let reminder = try fetchReminder(candidateIDs: candidateIDs)
        let save = try saveRequest()
        guard let change = save.perform(NSSelectorFromString("updateReminder:"), with: reminder)?.takeUnretainedValue() as? NSObject else {
            throw HelperError("Could not update Reminders private metadata", code: "private_reminder_update_failed")
        }
        if fields.keys.contains("tags") {
            let tags = stringArray(fields["tags"])
            try replaceTags(change: change, tags: tags)
        }
        if fields.keys.contains("subtasks") {
            let subtasks = objectArray(fields["subtasks"])
            if subtasks.isEmpty {
                try deleteSubtasks(reminder: reminder, save: save)
            } else {
                try addSubtasks(save: save, change: change, subtasks: subtasks)
            }
        }
        if fields.keys.contains("attachments") {
            try replaceReminderAttachments(change: change, attachments: objectArray(fields["attachments"]))
        }
        if fields.keys.contains("alarms") {
            try replaceDueDateDeltaAlerts(
                change: change,
                alarms: objectArray(fields["alarms"]),
                candidateIDs: candidateIDs
            )
        }
        if fields.keys.contains("recurrence") {
            try replaceReminderRecurrence(change: change, recurrence: object(fields["recurrence"]))
        }
        if fields.keys.contains("section_id") {
            let sectionID = string(fields["section_id"])
            if sectionID.isEmpty {
                try clearSection(reminder: reminder, save: save)
            } else {
                try assignSection(reminder: reminder, save: save, sectionID: sectionID)
            }
        }
        if fields.keys.contains("parent_id") {
            let parentID = string(fields["parent_id"])
            if parentID.isEmpty {
                removeFromParentReminder(change: change)
            } else {
                try moveToParentReminder(change: change, save: save, parentID: parentID)
            }
        }
        if fields["flagged"] != nil {
            try setFlagged(change: change, flagged: bool(fields["flagged"], defaultValue: false))
        }
        if fields["urgent"] != nil {
            try setUrgentState(change: change, urgent: bool(fields["urgent"], defaultValue: false))
        }
        try saveSynchronously(save)
    }

    func fetchReminder(candidateIDs: [String]) throws -> NSObject {
        for candidate in candidateIDs {
            for objectID in objectIDs(kind: "REMCDReminder", rawIdentifier: candidate) {
                let result = callWithError(remStore, "fetchReminderWithObjectID:error:", [objectID])
                if let reminder = result.0 as? NSObject {
                    return reminder
                }
            }
        }
        throw HelperError("Reminders item not found in ReminderKit", code: "not_found")
    }

    func attachments(candidateIDs: [String]) throws -> [[String: Any]] {
        guard Self.supportsAttachments else {
            return []
        }
        let reminder = try fetchReminder(candidateIDs: candidateIDs)
        guard let context = reminder.perform(NSSelectorFromString("attachmentContext"))?.takeUnretainedValue() as? NSObject,
              let raw = context.perform(NSSelectorFromString("attachments"))?.takeUnretainedValue() as? NSArray else {
            return []
        }
        return raw.compactMap { value in
            guard let attachment = value as? NSObject else {
                return nil
            }
            return serializeReminderAttachment(attachment)
        }
    }

    func serializeReminderAttachment(_ attachment: NSObject) -> [String: Any] {
        let info = objectIDInfo(remObjectID(attachment))
        let className = String(describing: type(of: attachment))
        let fileURL = urlFromSelector(attachment, "fileURL")
        let webURL = urlFromSelector(attachment, "url")
        let kind: String
        if webURL != nil {
            kind = "url"
        } else if className.lowercased().contains("image") {
            kind = "image"
        } else {
            kind = "file"
        }
        var payload = compact([
            "id": info.id,
            "reminderkit_url": info.url,
            "type": kind,
            "file_name": fileURL?.lastPathComponent ?? "",
            "file_path": fileURL?.isFileURL == true ? fileURL?.path ?? "" : "",
            "file_url": fileURL?.absoluteString ?? "",
            "url": webURL?.absoluteString ?? "",
            "uti": stringFromSelector(attachment, "uti"),
        ])
        if let fileSize = getUInt64(attachment, selectorName: "fileSize") {
            payload["file_size"] = fileSize
        }
        if let width = getUInt64(attachment, selectorName: "width") {
            payload["width"] = width
        }
        if let height = getUInt64(attachment, selectorName: "height") {
            payload["height"] = height
        }
        return payload
    }

    func fetchSection(id: String) -> NSObject? {
        for objectID in objectIDs(kind: "REMCDListSection", rawIdentifier: id) {
            let result = callWithError(remStore, "fetchListSectionWithObjectID:error:", [objectID])
            if let section = result.0 as? NSObject {
                return section
            }
        }
        return nil
    }

    func findList(calendar: EKCalendar?, title: String) throws -> NSObject {
        let titleCandidates = uniqueStrings([title, calendar?.title ?? ""])
        let idCandidates = uniqueStrings([calendar?.calendarIdentifier ?? ""])
        for candidate in idCandidates {
            for objectID in objectIDs(kind: "REMCDList", rawIdentifier: candidate) {
                let result = callWithError(remStore, "fetchListWithObjectID:error:", [objectID])
                if let list = result.0 as? NSObject {
                    return list
                }
            }
        }
        guard let accountsView = accountsView, let listsView = listsView else {
            throw HelperError("ReminderKit list data views are unavailable", code: "unsupported_private_feature")
        }
        let accountsResult = callWithError(accountsView, "fetchAllAccountsWithError:")
        guard let accounts = accountsResult.0 as? NSArray else {
            throw HelperError("Could not read Reminders accounts", code: "private_list_lookup_failed")
        }
        for account in accounts {
            guard let accountObject = account as? NSObject else {
                continue
            }
            let listsResult = callWithError(listsView, "fetchListsInAccount:error:", [accountObject])
            guard let lists = listsResult.0 as? NSArray else {
                continue
            }
            for list in lists {
                guard let listObject = list as? NSObject else {
                    continue
                }
                let displayName = string(safeValue(listObject, key: "displayName"))
                if titleCandidates.contains(displayName) {
                    return listObject
                }
            }
        }
        throw HelperError("Reminders list not found: \(titleCandidates.joined(separator: ", "))", code: "not_found")
    }

    func saveRequest() throws -> NSObject {
        guard let cls = NSClassFromString("REMSaveRequest") as? NSObject.Type,
              let request = cls.perform(NSSelectorFromString("alloc"))?.takeUnretainedValue() as? NSObject else {
            throw HelperError("REMSaveRequest is unavailable", code: "unsupported_private_feature")
        }
        _ = request.perform(NSSelectorFromString("initWithStore:"), with: remStore)
        return request
    }

    func saveSynchronously(_ request: NSObject) throws {
        let result = callBoolWithError(request, "saveSynchronouslyWithError:")
        if !result.ok {
            throw HelperError(result.error?.localizedDescription ?? "ReminderKit save failed", code: "private_save_failed")
        }
    }

    func replaceTags(change: NSObject, tags: [String]) throws {
        guard let context = change.perform(NSSelectorFromString("hashtagContext"))?.takeUnretainedValue() as? NSObject else {
            throw HelperError("ReminderKit hashtag context is unavailable", code: "unsupported_private_feature")
        }
        _ = context.perform(NSSelectorFromString("removeAllHashtags"))
        for tag in uniqueStrings(tags) where !tag.isEmpty {
            guard callIntObjectMethod(context, selectorName: "addHashtagWithType:name:", intValue: 1, object: tag as NSString) != nil else {
                throw HelperError("Could not add Reminders tag: \(tag)", code: "private_tag_failed")
            }
        }
    }

    func addSubtasks(save: NSObject, change: NSObject, subtasks: [[String: Any]]) throws {
        guard let context = change.perform(NSSelectorFromString("subtaskContext"))?.takeUnretainedValue() as? NSObject else {
            throw HelperError("ReminderKit subtask context is unavailable", code: "unsupported_private_feature")
        }
        for subtask in subtasks {
            let title = try requiredTitle(subtask)
            guard save.perform(
                NSSelectorFromString("addReminderWithTitle:toReminderSubtaskContextChangeItem:"),
                with: title as NSString,
                with: context
            )?.takeUnretainedValue() != nil else {
                throw HelperError("Could not create Reminders subtask: \(title)", code: "private_subtask_create_failed")
            }
        }
    }

    func setUrgentState(change: NSObject, urgent: Bool) throws {
        guard let context = change.perform(NSSelectorFromString("urgentAlarmContext"))?.takeUnretainedValue() as? NSObject,
              context.responds(to: NSSelectorFromString("setIsUrgentStateEnabledForCurrentUser:")) else {
            throw HelperError("ReminderKit urgent context is unavailable", code: "unsupported_private_feature")
        }
        callBoolSetter(context, selectorName: "setIsUrgentStateEnabledForCurrentUser:", value: urgent)
    }

    func replaceDueDateDeltaAlerts(
        change: NSObject,
        alarms: [[String: Any]],
        candidateIDs: [String]
    ) throws {
        guard let context = change.perform(NSSelectorFromString("dueDateDeltaAlertContext"))?.takeUnretainedValue() as? NSObject else {
            throw HelperError("ReminderKit due-date delta alert context is unavailable", code: "unsupported_private_feature")
        }
        let existingIdentifiers = dueDateDeltaAlertIdentifiers(candidateIDs: candidateIDs)
        let uuids = existingIdentifiers.compactMap { UUID(uuidString: $0) as NSUUID? }
        if !uuids.isEmpty,
           context.responds(to: NSSelectorFromString("removeDueDateDeltaAlertsWithIdentifiers:")) {
            _ = context.perform(
                NSSelectorFromString("removeDueDateDeltaAlertsWithIdentifiers:"),
                with: uuids as NSArray
            )
        }
        _ = context.perform(NSSelectorFromString("removeAllFetchedDueDateDeltaAlerts"))
        _ = context.perform(NSSelectorFromString("clearPendingDueDateDeltaAlertUpserts"))
        for spec in alarms {
            guard let interval = dueDateDeltaInterval(from: spec) else {
                throw HelperError("Reminders early reminders require a relative before-due alarm", code: "invalid_alarm")
            }
            guard context.perform(NSSelectorFromString("addDueDateDeltaAlertWithDueDateDelta:"), with: interval)?.takeUnretainedValue() != nil else {
                throw HelperError("Could not add Reminders early reminder", code: "private_alarm_failed")
            }
        }
    }

    func dueDateDeltaInterval(from spec: [String: Any]) -> NSObject? {
        if let minutes = doubleValue(spec["minutes_before"]) {
            return makeDueDateDeltaInterval(unit: 0, count: -Int64(abs(minutes).rounded()))
        }
        return nil
    }

    func makeDueDateDeltaInterval(unit: Int64, count: Int64) -> NSObject? {
        guard let cls = NSClassFromString("REMDueDateDeltaInterval") as? NSObject.Type,
              let object = cls.perform(NSSelectorFromString("alloc"))?.takeUnretainedValue() as? NSObject else {
            return nil
        }
        let selector = NSSelectorFromString("initWithUnit:count:")
        guard let method = class_getInstanceMethod(cls, selector) else {
            return nil
        }
        let fn = unsafeBitCast(
            method_getImplementation(method),
            to: (@convention(c) (AnyObject, Selector, Int64, Int64) -> AnyObject?).self
        )
        return fn(object, selector, unit, count) as? NSObject
    }

    func replaceReminderRecurrence(change: NSObject, recurrence: [String: Any]) throws {
        _ = change.perform(NSSelectorFromString("removeAllRecurrenceRules"))
        if recurrence.isEmpty {
            return
        }
        guard let frequency = reminderRecurrenceFrequency(recurrence["frequency"]) else {
            return
        }
        let interval = max(1, int64(recurrence["interval"]))
        let end = reminderRecurrenceEnd(recurrence)
        let selector = NSSelectorFromString("addRecurrenceRuleWithFrequency:interval:firstDayOfTheWeek:daysOfTheWeek:daysOfTheMonth:monthsOfTheYear:weeksOfTheYear:daysOfTheYear:setPositions:end:")
        guard let method = class_getInstanceMethod(type(of: change), selector) else {
            throw HelperError("ReminderKit recurrence writer is unavailable", code: "unsupported_private_feature")
        }
        let fn = unsafeBitCast(
            method_getImplementation(method),
            to: (@convention(c) (
                AnyObject,
                Selector,
                Int64,
                Int64,
                Int64,
                AnyObject?,
                AnyObject?,
                AnyObject?,
                AnyObject?,
                AnyObject?,
                AnyObject?,
                AnyObject?
            ) -> AnyObject?).self
        )
        _ = fn(
            change,
            selector,
            frequency,
            interval,
            2,
            reminderRecurrenceDaysOfWeek(recurrence["days_of_week"]) as NSArray?,
            nsNumbers(recurrence["days_of_month"]) as NSArray?,
            nsNumbers(recurrence["months_of_year"]) as NSArray?,
            nil,
            nil,
            nil,
            end
        )
    }

    func reminderRecurrenceFrequency(_ value: Any?) -> Int64? {
        switch string(value).lowercased() {
        case "daily":
            return 0
        case "weekly":
            return 1
        case "monthly":
            return 2
        case "yearly":
            return 3
        default:
            return nil
        }
    }

    func reminderRecurrenceEnd(_ spec: [String: Any]) -> NSObject? {
        if let count = UInt64(exactly: int64(spec["occurrence_count"])), count > 0 {
            return makeReminderRecurrenceEndWithCount(count)
        }
        if let date = parseDate(spec["end_date"]) {
            return makeReminderRecurrenceEndWithDate(date)
        }
        return nil
    }

    func makeReminderRecurrenceEndWithDate(_ date: Date) -> NSObject? {
        guard let cls = NSClassFromString("REMRecurrenceEnd") as? NSObject.Type,
              let object = cls.perform(NSSelectorFromString("alloc"))?.takeUnretainedValue() as? NSObject else {
            return nil
        }
        return object.perform(NSSelectorFromString("initWithEndDate:"), with: date as NSDate)?.takeUnretainedValue() as? NSObject
    }

    func makeReminderRecurrenceEndWithCount(_ count: UInt64) -> NSObject? {
        guard let cls = NSClassFromString("REMRecurrenceEnd") as? NSObject.Type,
              let object = cls.perform(NSSelectorFromString("alloc"))?.takeUnretainedValue() as? NSObject else {
            return nil
        }
        let selector = NSSelectorFromString("initWithOccurrenceCount:")
        guard let method = class_getInstanceMethod(cls, selector) else {
            return nil
        }
        let fn = unsafeBitCast(
            method_getImplementation(method),
            to: (@convention(c) (AnyObject, Selector, UInt64) -> AnyObject?).self
        )
        return fn(object, selector, count) as? NSObject
    }

    func reminderRecurrenceDaysOfWeek(_ value: Any?) -> [NSObject]? {
        let values = arrayValue(value)
        if values.isEmpty {
            return nil
        }
        let days = values.compactMap(reminderRecurrenceDayOfWeek)
        return days.isEmpty ? nil : days
    }

    func reminderRecurrenceDayOfWeek(_ value: Any) -> NSObject? {
        let raw: Any
        let weekNumber: Int64
        if let payload = value as? [String: Any] {
            raw = payload["day"] ?? ""
            weekNumber = int64(payload["week_number"])
        } else {
            raw = value
            weekNumber = 0
        }
        guard let weekday = recurrenceWeekdayNumber(raw) else {
            return nil
        }
        guard let cls = NSClassFromString("REMRecurrenceDayOfWeek") as? NSObject.Type,
              let object = cls.perform(NSSelectorFromString("alloc"))?.takeUnretainedValue() as? NSObject else {
            return nil
        }
        let selector = NSSelectorFromString("initWithDayOfTheWeek:weekNumber:")
        guard let method = class_getInstanceMethod(cls, selector) else {
            return nil
        }
        let fn = unsafeBitCast(
            method_getImplementation(method),
            to: (@convention(c) (AnyObject, Selector, Int64, Int64) -> AnyObject?).self
        )
        return fn(object, selector, weekday, weekNumber) as? NSObject
    }

    func replaceReminderAttachments(change: NSObject, attachments: [[String: Any]]) throws {
        guard Self.supportsAttachments else {
            throw HelperError("ReminderKit attachment backend is unavailable", code: "unsupported_private_feature")
        }
        guard let context = change.perform(NSSelectorFromString("attachmentContext"))?.takeUnretainedValue() as? NSObject else {
            throw HelperError("ReminderKit attachment context is unavailable", code: "unsupported_private_feature")
        }
        _ = context.perform(NSSelectorFromString("removeAllAttachments"))
        for spec in attachments {
            if let url = attachmentURL(spec) {
                try addReminderImageAttachment(context: context, url: url)
                continue
            }
            if let webURL = webAttachmentURL(spec) {
                guard context.perform(NSSelectorFromString("addURLAttachmentWithURL:"), with: webURL as NSURL)?.takeUnretainedValue() != nil else {
                    throw HelperError("Could not add Reminders URL attachment: \(webURL.absoluteString)", code: "private_attachment_failed")
                }
                continue
            }
            throw HelperError("attachment file_path or url is required", code: "invalid_attachment")
        }
    }

    func addReminderImageAttachment(context: NSObject, url: URL) throws {
        guard let image = NSImage(contentsOf: url) else {
            throw HelperError(
                "Reminders only supports image file attachments in the visible UI: \(url.path)",
                code: "unsupported_attachment_type"
            )
        }
        let pixelSize = imagePixelSize(image)
        let selector = NSSelectorFromString("addImageAttachmentWithURL:width:height:error:")
        guard let method = class_getInstanceMethod(type(of: context), selector) else {
            throw HelperError("ReminderKit image attachment backend is unavailable", code: "unsupported_private_feature")
        }
        let fn = unsafeBitCast(
            method_getImplementation(method),
            to: (@convention(c) (
                AnyObject,
                Selector,
                AnyObject,
                UInt64,
                UInt64,
                UnsafeMutablePointer<NSError?>
            ) -> AnyObject?).self
        )
        var error: NSError?
        let result = fn(
            context,
            selector,
            url as NSURL,
            UInt64(max(1, pixelSize.width)),
            UInt64(max(1, pixelSize.height)),
            &error
        )
        if result == nil {
            throw HelperError(error?.localizedDescription ?? "Could not add Reminders image attachment: \(url.path)", code: "private_attachment_failed")
        }
    }

    func imagePixelSize(_ image: NSImage) -> (width: Int, height: Int) {
        for representation in image.representations {
            if representation.pixelsWide > 0 && representation.pixelsHigh > 0 {
                return (representation.pixelsWide, representation.pixelsHigh)
            }
        }
        return (Int(max(1, image.size.width.rounded())), Int(max(1, image.size.height.rounded())))
    }

    func attachmentURL(_ spec: [String: Any]) -> URL? {
        let path = string(spec["file_path"] ?? spec["path"] ?? spec["local_path"])
        if path.isEmpty {
            return nil
        }
        let expanded = NSString(string: path).expandingTildeInPath
        guard FileManager.default.fileExists(atPath: expanded) else {
            return nil
        }
        return URL(fileURLWithPath: expanded)
    }

    func webAttachmentURL(_ spec: [String: Any]) -> URL? {
        let text = string(spec["url"])
        if text.isEmpty {
            return nil
        }
        guard let url = URL(string: text), ["http", "https"].contains(url.scheme?.lowercased() ?? "") else {
            return nil
        }
        return url
    }

    func deleteSubtasks(reminder: NSObject, save: NSObject) throws {
        guard let reminderObjectID = remObjectID(reminder) else {
            throw HelperError("Could not resolve Reminders object id", code: "private_subtask_delete_failed")
        }
        let info = objectIDInfo(reminderObjectID)
        let childIDs = SQLiteReminderSnapshot.load(calendarListIDsByTitle: [:]).childIDs(forParentID: info.id)
        for childID in childIDs {
            let child = try fetchReminder(candidateIDs: [childID])
            guard let childChange = save.perform(NSSelectorFromString("updateReminder:"), with: child)?.takeUnretainedValue() as? NSObject else {
                throw HelperError("Could not update Reminders subtask for delete: \(childID)", code: "private_subtask_delete_failed")
            }
            _ = childChange.perform(NSSelectorFromString("removeFromList"))
        }
    }

    func assignSection(reminder: NSObject, save: NSObject, sectionID: String) throws {
        let sectionObjectIDs = objectIDs(kind: "REMCDListSection", rawIdentifier: sectionID)
        guard let sectionObjectID = sectionObjectIDs.first else {
            throw HelperError("section_id is invalid: \(sectionID)", code: "invalid_section")
        }
        let result = callWithError(remStore, "fetchListSectionWithObjectID:error:", [sectionObjectID])
        guard let section = result.0 as? NSObject else {
            throw HelperError(result.1?.localizedDescription ?? "Reminders section not found: \(sectionID)", code: "not_found")
        }
        guard let list = section.perform(NSSelectorFromString("list"))?.takeUnretainedValue() as? NSObject,
              let listChange = save.perform(NSSelectorFromString("updateList:"), with: list)?.takeUnretainedValue() as? NSObject,
              let sectionContext = listChange.perform(NSSelectorFromString("sectionsContextChangeItem"))?.takeUnretainedValue() as? NSObject,
              let reminderObjectID = remObjectID(reminder),
              let membership = makeMembership(memberObjectID: reminderObjectID, groupObjectID: sectionObjectID),
              let memberships = makeMemberships([membership]) else {
            throw HelperError("Could not assign reminder to section", code: "private_section_assign_failed")
        }
        _ = sectionContext.perform(NSSelectorFromString("setUnsavedMembershipsOfRemindersInSections:"), with: memberships)
    }

    func clearSection(reminder: NSObject, save: NSObject) throws {
        guard let reminderObjectID = remObjectID(reminder) else {
            throw HelperError("Could not resolve Reminders object id", code: "private_section_assign_failed")
        }
        let info = objectIDInfo(reminderObjectID)
        let snapshot = SQLiteReminderSnapshot.load(calendarListIDsByTitle: [:])
        let sectionID = snapshot.metadata(forIdentifiers: [info.id, info.url])?.sectionID ?? ""
        if sectionID.isEmpty {
            return
        }
        let sectionObjectIDs = objectIDs(kind: "REMCDListSection", rawIdentifier: sectionID)
        guard let sectionObjectID = sectionObjectIDs.first else {
            throw HelperError("Could not resolve Reminders section assignment", code: "private_section_assign_failed")
        }
        let result = callWithError(remStore, "fetchListSectionWithObjectID:error:", [sectionObjectID])
        guard let section = result.0 as? NSObject,
              let list = section.perform(NSSelectorFromString("list"))?.takeUnretainedValue() as? NSObject,
              let listChange = save.perform(NSSelectorFromString("updateList:"), with: list)?.takeUnretainedValue() as? NSObject,
              let sectionContext = listChange.perform(NSSelectorFromString("sectionsContextChangeItem"))?.takeUnretainedValue() as? NSObject,
              let membership = makeMembership(memberObjectID: reminderObjectID, groupObjectID: sectionObjectID, isObsolete: true),
              let memberships = makeMemberships([membership]) else {
            throw HelperError("Could not clear Reminders section assignment", code: "private_section_assign_failed")
        }
        _ = sectionContext.perform(NSSelectorFromString("setUnsavedMembershipsOfRemindersInSections:"), with: memberships)
    }

    func setFlagged(change: NSObject, flagged: Bool) throws {
        guard let context = change.perform(NSSelectorFromString("flaggedContext"))?.takeUnretainedValue() as? NSObject else {
            throw HelperError("ReminderKit flagged context is unavailable", code: "unsupported_private_feature")
        }
        callIntSetter(context, selectorName: "setFlagged:", value: flagged ? 1 : 0)
    }

    func removeFromParentReminder(change: NSObject) {
        _ = change.perform(NSSelectorFromString("removeFromParentReminder"))
    }

    func moveToParentReminder(change: NSObject, save: NSObject, parentID: String) throws {
        let parent = try fetchReminder(candidateIDs: [parentID])
        guard let parentChange = save.perform(NSSelectorFromString("updateReminder:"), with: parent)?.takeUnretainedValue() as? NSObject,
              let subtaskContext = parentChange.perform(NSSelectorFromString("subtaskContext"))?.takeUnretainedValue() as? NSObject else {
            throw HelperError("Could not prepare Reminders parent move", code: "private_parent_assign_failed")
        }
        _ = subtaskContext.perform(NSSelectorFromString("addReminderChangeItem:"), with: change)
    }

    func sectionPayload(_ section: NSObject) -> [String: Any] {
        let info = objectIDInfo(remObjectID(section) ?? safeValue(section, key: "objectID") as AnyObject?)
        return compact([
            "id": info.id,
            "type": "section",
            "title": string(safeValue(section, key: "displayName")),
            "url": info.url,
        ])
    }
}

func requestAccess(reminders: Bool, events: Bool) throws {
    if reminders {
        try requestAccess(entity: .reminder)
    }
    if events {
        try requestAccess(entity: .event)
    }
}

func requestAccessForList(filters: [String: Any]) throws {
    let kind = string(filters["kind"]).lowercased()
    switch kind {
    case "reminder":
        try requestAccess(reminders: true, events: false)
    case "event":
        try requestAccess(reminders: false, events: true)
    default:
        try requestAccess(reminders: true, events: false)
        try? requestAccess(entity: .event)
    }
}

func requestAccessForCreate(items: [[String: Any]]) throws {
    var needsReminders = false
    var needsEvents = false
    for item in items {
        switch string(item["type"]) {
        case "reminder_list", "section", "reminder":
            needsReminders = true
        case "event_calendar", "event":
            needsEvents = true
        default:
            break
        }
    }
    try requestAccess(reminders: needsReminders, events: needsEvents)
}

func requestAccessForUpdates(updates: [[String: Any]]) throws {
    var needsReminders = false
    var needsEvents = false
    for update in updates {
        switch string(update["type"]) {
        case "event_calendar", "event":
            needsEvents = true
        case "reminder_list", "section", "reminder":
            needsReminders = true
        default:
            needsReminders = true
        }
    }
    try requestAccess(reminders: needsReminders, events: needsEvents)
}

func requestAccess(entity: EKEntityType) throws {
    let status = EKEventStore.authorizationStatus(for: entity)
    if #available(macOS 14.0, *) {
        if status == .fullAccess {
            return
        }
    } else {
        if status == .authorized {
            return
        }
    }
    if status == .denied || status == .restricted {
        throw HelperError(
            "Apple \(entityLabel(entity)) access is denied",
            code: "permission_denied",
            remediation: "Grant Calendar/Reminders access to the host app in macOS Privacy settings."
        )
    }
    let semaphore = DispatchSemaphore(value: 0)
    var granted = false
    var requestError: Error?
    if #available(macOS 14.0, *) {
        if entity == .event {
            store.requestFullAccessToEvents { ok, error in
                granted = ok
                requestError = error
                semaphore.signal()
            }
        } else {
            store.requestFullAccessToReminders { ok, error in
                granted = ok
                requestError = error
                semaphore.signal()
            }
        }
    } else {
        store.requestAccess(to: entity) { ok, error in
            granted = ok
            requestError = error
            semaphore.signal()
        }
    }
    semaphore.wait()
    if let requestError = requestError {
        throw requestError
    }
    if !granted {
        throw HelperError(
            "Apple \(entityLabel(entity)) access was not granted",
            code: "permission_denied",
            remediation: "Grant Calendar/Reminders access to the host app in macOS Privacy settings."
        )
    }
}

func reminderCalendar(id: String) -> EKCalendar {
    if !id.isEmpty, let calendar = store.calendar(withIdentifier: id) {
        return calendar
    }
    if let calendar = store.defaultCalendarForNewReminders() {
        return calendar
    }
    if let calendar = store.calendars(for: .reminder).first {
        return calendar
    }
    fatalError("No Reminders list is available")
}

func eventCalendar(id: String) -> EKCalendar {
    if !id.isEmpty, let calendar = store.calendar(withIdentifier: id) {
        return calendar
    }
    if let calendar = store.defaultCalendarForNewEvents {
        return calendar
    }
    if let calendar = store.calendars(for: .event).first {
        return calendar
    }
    fatalError("No Calendar is available")
}

func defaultSource(for entity: EKEntityType) throws -> EKSource {
    let calendars = store.calendars(for: entity)
    if let source = calendars.first?.source {
        return source
    }
    if let source = store.sources.first {
        return source
    }
    throw HelperError("No Apple source is available", code: "missing_source")
}

func eventDateRange(filters: [String: Any]) -> (start: Date, end: Date) {
    let calendar = Calendar.current
    let now = Date()
    let explicitStartText = string(filters["event_start"])
    let windowStart = eventWindowStart(filters: filters, now: now, calendar: calendar)
    let start = parseDate(explicitStartText) ?? windowStart
    if let explicitEnd = parseDate(string(filters["event_end"])) {
        return (start, explicitEnd)
    }
    if explicitStartText.isEmpty {
        let windowEnd = eventWindowEnd(filters: filters, start: start, now: now, calendar: calendar)
        return (start, windowEnd)
    }
    let end = calendar.date(byAdding: .day, value: 7, to: start) ?? now.addingTimeInterval(7 * 86400)
    return (start, end)
}

func eventWindowStart(filters: [String: Any], now: Date, calendar: Calendar) -> Date {
    let today = calendar.startOfDay(for: now)
    switch string(filters["event_window"]).lowercased() {
    case "tomorrow":
        return calendar.date(byAdding: .day, value: 1, to: today) ?? today
    default:
        return today
    }
}

func eventWindowEnd(filters: [String: Any], start: Date, now: Date, calendar: Calendar) -> Date {
    switch string(filters["event_window"]).lowercased() {
    case "today":
        return calendar.date(byAdding: .day, value: 1, to: start) ?? now.addingTimeInterval(86400)
    case "tomorrow":
        return calendar.date(byAdding: .day, value: 1, to: start) ?? now.addingTimeInterval(2 * 86400)
    case "week", "upcoming", "next_7_days", "":
        return calendar.date(byAdding: .day, value: 7, to: start) ?? now.addingTimeInterval(7 * 86400)
    default:
        return calendar.date(byAdding: .day, value: 7, to: start) ?? now.addingTimeInterval(7 * 86400)
    }
}

func requiredTitle(_ item: [String: Any]) throws -> String {
    let title = string(item["title"])
    if title.isEmpty {
        throw HelperError("title is required", code: "invalid_title")
    }
    return title
}

func reminderPriority(_ value: Any) -> Int {
    max(0, min(Int(int64(value)), 9))
}

func replaceAlarms(_ item: EKCalendarItem, alarms: [[String: Any]]) {
    for alarm in item.alarms ?? [] {
        item.removeAlarm(alarm)
    }
    for spec in alarms {
        if let alarm = alarmFromSpec(spec) {
            item.addAlarm(alarm)
        }
    }
}

func alarmFromSpec(_ spec: [String: Any]) -> EKAlarm? {
    if let date = parseDate(spec["absolute_at"])
        ?? parseDate(spec["at"])
        ?? parseDate(spec["date"])
        ?? parseDate(spec["date_at"]) {
        let alarm = EKAlarm(absoluteDate: date)
        applyAlarmFields(alarm, spec: spec)
        return alarm
    }
    let minutes = doubleValue(spec["minutes_before"])
    if let minutes = minutes {
        let alarm = EKAlarm(relativeOffset: -minutes * 60.0)
        applyAlarmFields(alarm, spec: spec)
        return alarm
    }
    return nil
}

func applyAlarmFields(_ alarm: EKAlarm, spec: [String: Any]) {
    if bool(spec["urgent"], defaultValue: false) {
        alarm.soundName = urgentAlarmSoundName
    } else if let rawSound = spec["sound_name"] ?? spec["soundName"] {
        let sound = string(rawSound)
        alarm.soundName = sound.isEmpty ? nil : sound
    }
}

func replaceRecurrence(_ item: EKCalendarItem, recurrence: [String: Any]) {
    for rule in item.recurrenceRules ?? [] {
        item.removeRecurrenceRule(rule)
    }
    if recurrence.isEmpty {
        return
    }
    guard let frequency = recurrenceFrequency(string(recurrence["frequency"])) else {
        return
    }
    let interval = max(1, Int(int64(recurrence["interval"])))
    let end = recurrenceEnd(recurrence)
    let rule = EKRecurrenceRule(
        recurrenceWith: frequency,
        interval: interval,
        daysOfTheWeek: recurrenceDaysOfWeek(recurrence["days_of_week"]),
        daysOfTheMonth: nsNumbers(recurrence["days_of_month"]),
        monthsOfTheYear: nsNumbers(recurrence["months_of_year"]),
        weeksOfTheYear: nil,
        daysOfTheYear: nil,
        setPositions: nil,
        end: end
    )
    item.addRecurrenceRule(rule)
}

func recurrenceFrequency(_ value: String) -> EKRecurrenceFrequency? {
    switch value.lowercased() {
    case "daily":
        return .daily
    case "weekly":
        return .weekly
    case "monthly":
        return .monthly
    case "yearly":
        return .yearly
    default:
        return nil
    }
}

func recurrenceEnd(_ spec: [String: Any]) -> EKRecurrenceEnd? {
    let count = Int(int64(spec["occurrence_count"]))
    if count > 0 {
        return EKRecurrenceEnd(occurrenceCount: count)
    }
    if let date = parseDate(spec["end_date"]) {
        return EKRecurrenceEnd(end: date)
    }
    return nil
}

func recurrenceDaysOfWeek(_ value: Any?) -> [EKRecurrenceDayOfWeek]? {
    let values = arrayValue(value)
    if values.isEmpty {
        return nil
    }
    let days = values.compactMap(recurrenceDayOfWeek)
    return days.isEmpty ? nil : days
}

func recurrenceDayOfWeek(_ value: Any) -> EKRecurrenceDayOfWeek? {
    if let payload = value as? [String: Any] {
        let day = weekday(string(payload["day"]))
        guard let day = day else {
            return nil
        }
        let weekNumber = Int(int64(payload["week_number"]))
        return EKRecurrenceDayOfWeek(dayOfTheWeek: day, weekNumber: weekNumber)
    }
    if let day = weekday(string(value)) ?? weekday(int64(value)) {
        return EKRecurrenceDayOfWeek(dayOfTheWeek: day, weekNumber: 0)
    }
    return nil
}

func nsNumbers(_ value: Any?) -> [NSNumber]? {
    let numbers = arrayValue(value)
        .map { NSNumber(value: Int(int64($0))) }
        .filter { $0.intValue != 0 }
    return numbers.isEmpty ? nil : numbers
}

func weekday(_ value: Int64) -> EKWeekday? {
    switch value {
    case 1: return .sunday
    case 2: return .monday
    case 3: return .tuesday
    case 4: return .wednesday
    case 5: return .thursday
    case 6: return .friday
    case 7: return .saturday
    default: return nil
    }
}

func weekday(_ value: String) -> EKWeekday? {
    switch value.lowercased() {
    case "su", "sun", "sunday", "日", "周日", "星期日": return .sunday
    case "mo", "mon", "monday", "一", "周一", "星期一": return .monday
    case "tu", "tue", "tuesday", "二", "周二", "星期二": return .tuesday
    case "we", "wed", "wednesday", "三", "周三", "星期三": return .wednesday
    case "th", "thu", "thursday", "四", "周四", "星期四": return .thursday
    case "fr", "fri", "friday", "五", "周五", "星期五": return .friday
    case "sa", "sat", "saturday", "六", "周六", "星期六": return .saturday
    default: return nil
    }
}

func recurrenceWeekdayNumber(_ value: Any) -> Int64? {
    if let number = value as? NSNumber {
        let raw = number.int64Value
        return (1...7).contains(raw) ? raw : nil
    }
    let text = string(value)
    if let raw = Int64(text), (1...7).contains(raw) {
        return raw
    }
    return weekday(text).map { Int64($0.rawValue) }
}

func weekdayName(_ value: EKWeekday) -> String {
    switch value {
    case .sunday: return "sunday"
    case .monday: return "monday"
    case .tuesday: return "tuesday"
    case .wednesday: return "wednesday"
    case .thursday: return "thursday"
    case .friday: return "friday"
    case .saturday: return "saturday"
    @unknown default: return String(value.rawValue)
    }
}

func span(_ value: Any?) -> EKSpan {
    switch string(value).lowercased() {
    case "future", "all":
        return .futureEvents
    default:
        return .thisEvent
    }
}

func updateSpan(_ update: [String: Any], fields: [String: Any]) -> EKSpan {
    let explicit = string(update["series_scope"])
    if !explicit.isEmpty {
        return span(explicit)
    }
    if fields.keys.contains("recurrence")
        || fields.keys.contains("attachments")
        || fields.keys.contains("travel_time_minutes") {
        return .futureEvents
    }
    return .thisEvent
}

func deleteSpan(force: Bool) -> EKSpan {
    force ? .futureEvents : .thisEvent
}

func entityLabel(_ entity: EKEntityType) -> String {
    entity == .event ? "Calendar" : "Reminders"
}

func nsColor(named name: String) -> NSColor? {
    let text = string(name).lowercased()
    switch text {
    case "red", "红", "红色": return .systemRed
    case "orange", "橙", "橙色": return .systemOrange
    case "yellow", "黄", "黄色": return .systemYellow
    case "green", "绿", "绿色": return .systemGreen
    case "blue", "蓝", "蓝色": return .systemBlue
    case "purple", "紫", "紫色": return .systemPurple
    case "pink", "粉", "粉色": return .systemPink
    case "gray", "grey", "灰", "灰色": return .systemGray
    default: return nil
    }
}

func calendarColor(from value: Any?) throws -> NSColor? {
    let text = string(value)
    if text.isEmpty {
        return nil
    }
    if let color = nsColor(named: text) {
        return color
    }
    throw HelperError(
        "unsupported calendar/list color: \(text)",
        code: "invalid_color",
        remediation: "Use one of: red, orange, yellow, green, blue, purple, pink, gray."
    )
}

func dateComponents(from text: String) -> DateComponents? {
    dateComponents(from: text, dateOnly: isDateOnlyText(text))
}

func dateComponents(from text: String, dateOnly: Bool) -> DateComponents? {
    if text.isEmpty {
        return nil
    }
    let calendar = Calendar(identifier: .gregorian)
    if dateOnly {
        let parts = text.split(separator: "-").map(String.init)
        guard parts.count == 3,
              let year = Int(parts[0]),
              let month = Int(parts[1]),
              let day = Int(parts[2]) else {
            return nil
        }
        return DateComponents(calendar: calendar, timeZone: nil, year: year, month: month, day: day)
    }
    guard let date = parseDate(text) else {
        return nil
    }
    return calendar.dateComponents(in: TimeZone.current, from: date)
}

func isDateOnlyText(_ text: String) -> Bool {
    let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
    if trimmed.count != 10 {
        return false
    }
    let parts = trimmed.split(separator: "-")
    return parts.count == 3 && parts.allSatisfy { part in
        part.rangeOfCharacter(from: CharacterSet.decimalDigits.inverted) == nil
    }
}

func serializeDateComponents(_ components: DateComponents) -> (date: String, dateTime: String, hasTime: Bool) {
    let year = components.year ?? 1
    let month = components.month ?? 1
    let day = components.day ?? 1
    let dateText = String(format: "%04d-%02d-%02d", year, month, day)
    guard components.hour != nil || components.minute != nil || components.second != nil else {
        return (dateText, "", false)
    }
    let hour = components.hour ?? 0
    let minute = components.minute ?? 0
    let second = components.second ?? 0
    let dateTime = String(format: "%@T%02d:%02d:%02d", dateText, hour, minute, second)
    return (dateText, dateTime, true)
}

func parseDate(_ value: Any?) -> Date? {
    parseDate(string(value))
}

func parseDate(_ text: String) -> Date? {
    if text.isEmpty {
        return nil
    }
    if let date = isoFormatter.date(from: text) ?? isoParser.date(from: text) {
        return date
    }
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.timeZone = TimeZone.current
    formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
    if let date = formatter.date(from: text) {
        return date
    }
    formatter.dateFormat = "yyyy-MM-dd"
    return formatter.date(from: text)
}

func formatDate(_ date: Date?) -> String {
    guard let date = date else {
        return ""
    }
    return isoFormatter.string(from: date)
}

func decodeObject(_ data: Data) throws -> [String: Any] {
    if data.isEmpty {
        return [:]
    }
    let value = try JSONSerialization.jsonObject(with: data)
    guard let object = value as? [String: Any] else {
        throw HelperError("request JSON must be an object", code: "invalid_request")
    }
    return object
}

func writeJSON(_ payload: [String: Any]) {
    let data = try! JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

func object(_ value: Any?) -> [String: Any] {
    if isNull(value) {
        return [:]
    }
    return value as? [String: Any] ?? [:]
}

func objectArray(_ value: Any?) -> [[String: Any]] {
    if isNull(value) {
        return []
    }
    return value as? [[String: Any]] ?? []
}

func arrayValue(_ value: Any?) -> [Any] {
    if isNull(value) {
        return []
    }
    return value as? [Any] ?? []
}

func stringArray(_ value: Any?) -> [String] {
    if isNull(value) {
        return []
    }
    return value as? [String] ?? []
}

func string(_ value: Any?) -> String {
    if isNull(value) {
        return ""
    }
    if let value = value as? String {
        return value.trimmingCharacters(in: .whitespacesAndNewlines)
    }
    if let value = value {
        return String(describing: value).trimmingCharacters(in: .whitespacesAndNewlines)
    }
    return ""
}

func isNull(_ value: Any?) -> Bool {
    value == nil || value is NSNull
}

func bool(_ value: Any?, defaultValue: Bool) -> Bool {
    if let value = value as? Bool {
        return value
    }
    let text = string(value).lowercased()
    if ["1", "true", "yes", "on"].contains(text) {
        return true
    }
    if ["0", "false", "no", "off"].contains(text) {
        return false
    }
    return defaultValue
}

func compact(_ payload: [String: Any]) -> [String: Any] {
    payload.filter { _, value in
        if let text = value as? String {
            return !text.isEmpty
        }
        if let array = value as? [Any] {
            return !array.isEmpty
        }
        return true
    }
}

func int64(_ value: Any?) -> Int64 {
    if let value = value as? Int64 {
        return value
    }
    if let value = value as? Int {
        return Int64(value)
    }
    if let value = value as? NSNumber {
        return value.int64Value
    }
    return Int64(string(value)) ?? 0
}

func doubleValue(_ value: Any?) -> Double? {
    if let value = value as? Double {
        return value
    }
    if let value = value as? Float {
        return Double(value)
    }
    if let value = value as? Int {
        return Double(value)
    }
    if let value = value as? Int64 {
        return Double(value)
    }
    if let value = value as? NSNumber {
        return value.doubleValue
    }
    let text = string(value)
    if text.isEmpty {
        return nil
    }
    return Double(text)
}

func rowText(_ value: Any?) -> String? {
    if let text = value as? String {
        return text
    }
    if let data = value as? Data {
        return String(data: data, encoding: .utf8)
    }
    return nil
}

func normalizedIdentifier(_ value: String) -> String {
    let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
    if text.isEmpty {
        return ""
    }
    if let url = URL(string: text), let last = url.pathComponents.last, !last.isEmpty {
        return last.lowercased()
    }
    if let slash = text.split(separator: "/").last {
        return String(slash).lowercased()
    }
    return text.lowercased()
}

func identifierKeys(_ value: String) -> [String] {
    let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
    if text.isEmpty {
        return []
    }
    let normalized = normalizedIdentifier(text)
    var keys = [text.lowercased(), normalized]
    if !normalized.isEmpty {
        keys.append("x-apple-reminderkit://remcdreminder/\(normalized)")
        keys.append("x-apple-reminderkit://remcdlistsection/\(normalized)")
        keys.append("x-apple-reminderkit://remcdlist/\(normalized)")
    }
    return uniqueStrings(keys)
}

func uniqueStrings(_ values: [String]) -> [String] {
    var seen: Set<String> = []
    var result: [String] = []
    for value in values {
        let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.isEmpty {
            continue
        }
        let key = text.lowercased()
        if seen.insert(key).inserted {
            result.append(text)
        }
    }
    return result
}

func callWithError(_ object: NSObject, _ selectorName: String, _ args: [AnyObject] = []) -> (AnyObject?, NSError?) {
    let selector = NSSelectorFromString(selectorName)
    guard let method = class_getInstanceMethod(type(of: object), selector) else {
        return (nil, NSError(domain: "TelosReminderKit", code: 1, userInfo: [NSLocalizedDescriptionKey: "Method \(selectorName) not found"]))
    }
    let implementation = method_getImplementation(method)
    var error: NSError?
    switch args.count {
    case 0:
        let fn = unsafeBitCast(
            implementation,
            to: (@convention(c) (AnyObject, Selector, UnsafeMutablePointer<NSError?>) -> AnyObject?).self
        )
        return (fn(object, selector, &error), error)
    case 1:
        let fn = unsafeBitCast(
            implementation,
            to: (@convention(c) (AnyObject, Selector, AnyObject, UnsafeMutablePointer<NSError?>) -> AnyObject?).self
        )
        return (fn(object, selector, args[0], &error), error)
    default:
        return (nil, NSError(domain: "TelosReminderKit", code: 2, userInfo: [NSLocalizedDescriptionKey: "Unsupported argument count for \(selectorName)"]))
    }
}

func callBoolWithError(_ object: NSObject, _ selectorName: String) -> (ok: Bool, error: NSError?) {
    let selector = NSSelectorFromString(selectorName)
    guard let method = class_getInstanceMethod(type(of: object), selector) else {
        return (false, NSError(domain: "TelosReminderKit", code: 1, userInfo: [NSLocalizedDescriptionKey: "Method \(selectorName) not found"]))
    }
    let implementation = method_getImplementation(method)
    var error: NSError?
    let fn = unsafeBitCast(
        implementation,
        to: (@convention(c) (AnyObject, Selector, UnsafeMutablePointer<NSError?>) -> Bool).self
    )
    return (fn(object, selector, &error), error)
}

func callIntObjectMethod(_ object: NSObject, selectorName: String, intValue: Int, object argument: AnyObject) -> AnyObject? {
    let selector = NSSelectorFromString(selectorName)
    guard let method = class_getInstanceMethod(type(of: object), selector) else {
        return nil
    }
    let fn = unsafeBitCast(
        method_getImplementation(method),
        to: (@convention(c) (AnyObject, Selector, Int, AnyObject) -> AnyObject?).self
    )
    return fn(object, selector, intValue, argument)
}

func callIntSetter(_ object: NSObject, selectorName: String, value: Int) {
    let selector = NSSelectorFromString(selectorName)
    guard let method = class_getInstanceMethod(type(of: object), selector) else {
        return
    }
    let fn = unsafeBitCast(
        method_getImplementation(method),
        to: (@convention(c) (AnyObject, Selector, Int) -> Void).self
    )
    fn(object, selector, value)
}

func callBoolSetter(_ object: NSObject, selectorName: String, value: Bool) {
    let selector = NSSelectorFromString(selectorName)
    guard let method = class_getInstanceMethod(type(of: object), selector) else {
        return
    }
    let fn = unsafeBitCast(
        method_getImplementation(method),
        to: (@convention(c) (AnyObject, Selector, Bool) -> Void).self
    )
    fn(object, selector, value)
}

func callDoubleSetter(_ object: NSObject, selectorName: String, value: Double) {
    let selector = NSSelectorFromString(selectorName)
    guard let method = class_getInstanceMethod(type(of: object), selector) else {
        return
    }
    let fn = unsafeBitCast(
        method_getImplementation(method),
        to: (@convention(c) (AnyObject, Selector, Double) -> Void).self
    )
    fn(object, selector, value)
}

func getDouble(_ object: NSObject, selectorName: String) -> Double? {
    let selector = NSSelectorFromString(selectorName)
    guard let method = class_getInstanceMethod(type(of: object), selector) else {
        return nil
    }
    let fn = unsafeBitCast(
        method_getImplementation(method),
        to: (@convention(c) (AnyObject, Selector) -> Double).self
    )
    return fn(object, selector)
}

func getBool(_ object: NSObject, selectorName: String) -> Bool? {
    let selector = NSSelectorFromString(selectorName)
    guard let method = class_getInstanceMethod(type(of: object), selector) else {
        return nil
    }
    let fn = unsafeBitCast(
        method_getImplementation(method),
        to: (@convention(c) (AnyObject, Selector) -> Bool).self
    )
    return fn(object, selector)
}

func getDate(_ object: NSObject, selectorName: String) -> Date? {
    let selector = NSSelectorFromString(selectorName)
    guard object.responds(to: selector),
          let date = object.perform(selector)?.takeUnretainedValue() as? NSDate else {
        return nil
    }
    return date as Date
}

func runtimeSupports(_ cls: AnyClass, selectorName: String) -> Bool {
    class_getInstanceMethod(cls, NSSelectorFromString(selectorName)) != nil
}

func stringFromSelector(_ object: NSObject, _ selectorName: String) -> String {
    let selector = NSSelectorFromString(selectorName)
    guard object.responds(to: selector),
          let value = object.perform(selector)?.takeUnretainedValue() else {
        return ""
    }
    return string(value)
}

func urlStringFromSelector(_ object: NSObject, _ selectorName: String) -> String {
    let selector = NSSelectorFromString(selectorName)
    guard object.responds(to: selector),
          let value = object.perform(selector)?.takeUnretainedValue() else {
        return ""
    }
    if let url = value as? NSURL {
        return url.absoluteString ?? ""
    }
    return string(value)
}

func urlFromSelector(_ object: NSObject, _ selectorName: String) -> URL? {
    let selector = NSSelectorFromString(selectorName)
    guard object.responds(to: selector),
          let value = object.perform(selector)?.takeUnretainedValue() else {
        return nil
    }
    if let url = value as? URL {
        return url
    }
    if let url = value as? NSURL {
        return url as URL
    }
    return URL(string: string(value))
}

func getUInt64(_ object: NSObject, selectorName: String) -> UInt64? {
    let selector = NSSelectorFromString(selectorName)
    guard let method = class_getInstanceMethod(type(of: object), selector) else {
        return nil
    }
    let fn = unsafeBitCast(
        method_getImplementation(method),
        to: (@convention(c) (AnyObject, Selector) -> UInt64).self
    )
    return fn(object, selector)
}

func callStringSetter(_ object: NSObject, selectors: [String], value: String) -> Bool {
    for selectorName in selectors {
        let selector = NSSelectorFromString(selectorName)
        if object.responds(to: selector) {
            _ = object.perform(selector, with: value as NSString)
            return true
        }
    }
    return false
}

func objectIDs(kind: String, rawIdentifier: String) -> [NSObject] {
    let ids = identifierKeys(rawIdentifier)
    guard let objectIDClass = NSClassFromString("REMObjectID") else {
        return []
    }
    var result: [NSObject] = []
    for id in ids {
        let clean = normalizedIdentifier(id)
        let urls = uniqueStrings([
            rawIdentifier,
            id,
            "x-apple-reminderkit://\(kind)/\(clean)",
        ])
        for urlText in urls {
            guard let url = URL(string: urlText) else {
                continue
            }
            let unmanaged = (objectIDClass as AnyObject).perform(NSSelectorFromString("objectIDWithURL:"), with: url as NSURL)
            if let objectID = unmanaged?.takeUnretainedValue() as? NSObject {
                result.append(objectID)
            }
        }
    }
    return result
}

func remObjectID(_ object: NSObject) -> AnyObject? {
    if object.responds(to: NSSelectorFromString("remObjectID")) {
        return object.perform(NSSelectorFromString("remObjectID"))?.takeUnretainedValue()
    }
    if object.responds(to: NSSelectorFromString("objectID")) {
        return object.perform(NSSelectorFromString("objectID"))?.takeUnretainedValue()
    }
    return nil
}

func objectIDInfo(_ raw: Any?) -> (id: String, url: String) {
    guard let object = raw as? NSObject else {
        return ("", "")
    }
    var id = ""
    var url = ""
    if object.responds(to: NSSelectorFromString("uuid")),
       let uuid = object.perform(NSSelectorFromString("uuid"))?.takeUnretainedValue() as? NSUUID {
        id = uuid.uuidString
    }
    if object.responds(to: NSSelectorFromString("urlRepresentation")),
       let rawURL = object.perform(NSSelectorFromString("urlRepresentation"))?.takeUnretainedValue() as? NSURL {
        url = rawURL.absoluteString ?? ""
        if id.isEmpty {
            id = normalizedIdentifier(url).uppercased()
        }
    }
    return (id, url)
}

func makeMembership(memberObjectID: AnyObject, groupObjectID: AnyObject, isObsolete: Bool = false) -> NSObject? {
    guard let cls = NSClassFromString("REMMembership") as? NSObject.Type,
          let object = cls.perform(NSSelectorFromString("alloc"))?.takeUnretainedValue() as? NSObject,
          let memberUUID = (memberObjectID as AnyObject).perform(NSSelectorFromString("uuid"))?.takeUnretainedValue() as? NSUUID,
          let groupUUID = (groupObjectID as AnyObject).perform(NSSelectorFromString("uuid"))?.takeUnretainedValue() as? NSUUID else {
        return nil
    }
    let selector = NSSelectorFromString("initWithMemberIdentifier:groupIdentifier:isObsolete:modifiedOn:")
    guard let method = class_getInstanceMethod(cls, selector) else {
        return nil
    }
    let fn = unsafeBitCast(
        method_getImplementation(method),
        to: (@convention(c) (AnyObject, Selector, AnyObject, AnyObject, Bool, AnyObject) -> AnyObject).self
    )
    return fn(object, selector, memberUUID, groupUUID, isObsolete, NSDate()) as? NSObject
}

func makeMemberships(_ memberships: [NSObject]) -> NSObject? {
    guard let cls = NSClassFromString("REMMemberships") as? NSObject.Type,
          let object = cls.perform(NSSelectorFromString("alloc"))?.takeUnretainedValue() as? NSObject else {
        return nil
    }
    return object.perform(NSSelectorFromString("initWithMemberships:"), with: memberships as NSArray)?.takeUnretainedValue() as? NSObject
}

func safeValue(_ object: NSObject, key: String) -> Any? {
    object.value(forKey: key)
}

extension Dictionary where Key == String, Value == Any {
    func filteringKeys(_ keep: (String) -> Bool) -> [String: Any] {
        var result: [String: Any] = [:]
        for (key, value) in self where keep(key) {
            result[key] = value
        }
        return result
    }
}

extension NSColor {
    var hexString: String {
        guard let color = usingColorSpace(.sRGB) else {
            return ""
        }
        let red = Int(round(color.redComponent * 255))
        let green = Int(round(color.greenComponent * 255))
        let blue = Int(round(color.blueComponent * 255))
        return String(format: "#%02X%02X%02X", red, green, blue)
    }
}

main()
