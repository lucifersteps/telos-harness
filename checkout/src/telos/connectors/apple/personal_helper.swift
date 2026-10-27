import AppKit
import EventKit
import ObjectiveC
import Foundation
import CoreLocation
import MapKit

// 签名的无界面连接器持有 Apple 权限；私有功能通过隔离的原生适配器提供。
let store = EKEventStore()
// LaunchServices starts the app independently of DSH. Bound its lifetime even
// if the caller exits; a missing write receipt remains unknown in AppleService.
let deadlineTimer = DispatchSource.makeTimerSource(queue: .global())
if let index = CommandLine.arguments.firstIndex(of: "--telos-timeout"),
   CommandLine.arguments.indices.contains(index + 1),
   let seconds = Double(CommandLine.arguments[index + 1]), seconds > 0, seconds <= 300 {
    deadlineTimer.schedule(deadline: .now() + seconds)
    deadlineTimer.setEventHandler { _exit(124) }
    deadlineTimer.resume()
}
let formatter: ISO8601DateFormatter = {
    let value = ISO8601DateFormatter()
    value.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return value
}()

struct Failure: Error {
    let code: String
    let message: String
}

func emit(_ value: [String: Any]) {
    let data = try! JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([10]))
}

func text(_ value: Any?) -> String { value as? String ?? "" }
func ids(_ value: Any?) -> Set<String> { Set(value as? [String] ?? []) }
func stamp(_ date: Date?) -> String { date.map { formatter.string(from: $0) } ?? "" }
func date(_ value: Any?) throws -> Date {
    let input = text(value)
    if let result = formatter.date(from: input) ?? ISO8601DateFormatter().date(from: input) {
        return result
    }
    throw Failure(code: "invalid_date", message: "日期必须包含 ISO 8601 时间和时区。")
}

func permitted(_ entity: EKEntityType) -> Bool {
    let status = EKEventStore.authorizationStatus(for: entity)
    return status == .fullAccess
}

func permissionName(_ entity: EKEntityType) -> String {
    switch EKEventStore.authorizationStatus(for: entity) {
    case .notDetermined: return "not_determined"
    case .restricted: return "restricted"
    case .denied: return "denied"
    case .fullAccess: return "full_access"
    case .writeOnly: return "write_only"
    @unknown default: return "unknown"
    }
}

func permissionStatus() -> [String: Any] {
    ["events": permitted(.event), "reminders": permitted(.reminder),
     "event_status": permissionName(.event), "reminder_status": permissionName(.reminder),
     "application": Bundle.main.bundleIdentifier ?? "", "pid": ProcessInfo.processInfo.processIdentifier]
}

func requireAccess() throws {
    if !permitted(.event) || !permitted(.reminder) {
        throw Failure(code: "permission_denied", message: "请先为 Telos Apple 授权日历和提醒事项；已拒绝时，请在系统设置 → 隐私与安全性中修改。")
    }
}

func authorize() throws {
    var failures: [String] = []
    var timedOut = false
    for entity in [EKEntityType.event, .reminder] {
        if permitted(entity) { continue }
        let name = entity == .event ? "日历" : "提醒事项"
        if [.denied, .restricted].contains(EKEventStore.authorizationStatus(for: entity)) {
            failures.append("\(name)权限已拒绝或受系统限制，请在系统设置 → 隐私与安全性 → \(name)中检查 Telos Apple。")
            continue
        }
        let semaphore = DispatchSemaphore(value: 0)
        var granted = false
        var reason = ""
        let callback: (Bool, Error?) -> Void = { ok, error in granted = ok; reason = error?.localizedDescription ?? ""; semaphore.signal() }
        if entity == .event { store.requestFullAccessToEvents(completion: callback) }
        else { store.requestFullAccessToReminders(completion: callback) }
        if semaphore.wait(timeout: .now() + 120) == .timedOut {
            timedOut = true; failures.append("等待\(name)授权超时，请再次点击授权并处理 macOS 弹窗。")
        } else if !granted {
            failures.append("未获得\(name)访问权限。\(reason) 请在系统设置 → 隐私与安全性 → \(name)中检查 Telos Apple。")
        }
    }
    if !failures.isEmpty { throw Failure(code: timedOut ? "authorization_timeout" : "permission_denied", message: failures.joined(separator: "\n")) }
}

func calendarPayload(_ calendar: EKCalendar, kind: String) -> [String: Any] {
    ["id": calendar.calendarIdentifier, "title": calendar.title, "kind": kind,
     "writable": calendar.allowsContentModifications,
     "max_alarms": calendar.source?.sourceType == .exchange ? 1 : NSNull()]
}

func common(_ item: EKCalendarItem, kind: String, id: String) -> [String: Any] {
    ["id": id, "kind": kind, "calendar_id": item.calendar.calendarIdentifier,
     "title": item.title ?? "", "notes": item.notes ?? "", "url": item.url?.absoluteString ?? "",
     "modified_at": stamp(item.lastModifiedDate), "recurring": item.hasRecurrenceRules,
     "recurrence_rules": (item.recurrenceRules ?? []).map { recurrencePayload($0) },
     "external_identifier": item.calendarItemExternalIdentifier ?? ""]
}

func reminderPayload(_ reminder: EKReminder) throws -> [String: Any] {
    var result = common(reminder, kind: "reminder", id: reminder.calendarItemIdentifier)
    result["completed"] = reminder.isCompleted
    result["completed_at"] = stamp(reminder.completionDate)
    result["priority"] = reminder.priority
    result["alarms"] = alarmPayload(reminder)
    result["location"] = reminderLocation(reminder)
    result.merge(try PersonalReminderAlerts().state(reminder.calendarItemIdentifier)) { _, new in new }
    if let components = reminder.dueDateComponents {
        if components.hour == nil {
            result["due"] = String(format: "%04d-%02d-%02d", components.year ?? 0, components.month ?? 0, components.day ?? 0)
        } else {
            result["due"] = stamp((components.calendar ?? Calendar.current).date(from: components))
        }
    } else { result["due"] = "" }
    return result
}

func eventID(_ event: EKEvent) -> String {
    // Occurrences in a recurring series must not share a cache key.
    return (event.eventIdentifier ?? event.calendarItemIdentifier) + "|" + stamp(event.startDate)
}

func eventPayload(_ event: EKEvent) -> [String: Any] {
    var result = common(event, kind: "event", id: eventID(event))
    result["start"] = stamp(event.startDate)
    result["end"] = stamp(event.endDate)
    result["all_day"] = event.isAllDay
    result["time_zone"] = event.timeZone?.identifier ?? TimeZone.current.identifier
    result["location"] = event.location ?? ""
    result["alarm_minutes_before"] = (event.alarms ?? []).compactMap { alarm -> Int? in
        let value = -alarm.relativeOffset / 60
        guard alarm.absoluteDate == nil, alarm.structuredLocation == nil, value.isFinite, value >= 0,
              value <= 525600, value == value.rounded() else { return nil }
        return Int(value)
    }.sorted()
    result["travel_time_minutes"] = travelTime(event) as Any? ?? NSNull()
    result["availability"] = [0:"busy",1:"free",2:"tentative",3:"unavailable"][event.availability.rawValue] ?? "not_supported"
    result["event_status"] = event.status.rawValue
    result["attendees"] = (event.attendees ?? []).map { ["name":$0.name ?? "", "url":$0.url.absoluteString,"reply_status":$0.participantStatus.rawValue] }.sorted { text($0["url"]) < text($1["url"]) }
    result["alarms"] = alarmPayload(event)
    return result
}

func alarmPayload(_ item: EKCalendarItem) -> [[String: Any]] {
    (item.alarms ?? []).map { alarm in
        ["relative_offset": alarm.relativeOffset, "absolute_date": stamp(alarm.absoluteDate),
         "type": alarm.type.rawValue, "proximity": alarm.proximity.rawValue,
         "location": alarm.structuredLocation?.title ?? "", "radius": alarm.structuredLocation?.radius ?? 0,
         "latitude": alarm.structuredLocation?.geoLocation?.coordinate.latitude as Any? ?? NSNull(),
         "longitude": alarm.structuredLocation?.geoLocation?.coordinate.longitude as Any? ?? NSNull(),
         "email": alarm.emailAddress ?? "", "sound": alarm.soundName ?? ""]
    }.sorted { left, right in
        let a = try! JSONSerialization.data(withJSONObject: left, options: [.sortedKeys])
        let b = try! JSONSerialization.data(withJSONObject: right, options: [.sortedKeys])
        return a.lexicographicallyPrecedes(b)
    }
}

func alarmMinutes(_ value: Any, minimum: Int = 0) throws -> Int {
    guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
          number.doubleValue.isFinite, number.doubleValue == number.doubleValue.rounded(),
          number.doubleValue >= Double(minimum), number.doubleValue <= 525600 else {
        throw Failure(code: "invalid_field", message: "提前提醒分钟数必须是 \(minimum) 到 525600 的整数。")
    }
    return number.intValue
}

let recurrenceFrequencies: [String: EKRecurrenceFrequency] = ["daily":.daily, "weekly":.weekly, "monthly":.monthly, "yearly":.yearly]
let recurrenceWeekdays = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]

func recurrencePayload(_ rule: EKRecurrenceRule) -> [String: Any] {
    var result: [String: Any] = ["frequency": recurrenceFrequencies.first { $0.value == rule.frequency }!.key,
        "interval": rule.interval]
    if let days = rule.daysOfTheWeek, !days.isEmpty {
        if days.contains(where:{$0.weekNumber != 0}) {
            result["ordinal_weekdays"] = days.sorted { ($0.dayOfTheWeek.rawValue,$0.weekNumber) < ($1.dayOfTheWeek.rawValue,$1.weekNumber) }.map { ["day":recurrenceWeekdays[$0.dayOfTheWeek.rawValue-1],"week":$0.weekNumber] }
        } else { result["weekdays"] = days.map { recurrenceWeekdays[$0.dayOfTheWeek.rawValue - 1] }.sorted() }
    }
    if let days = rule.daysOfTheMonth, !days.isEmpty { result["month_days"] = days.sorted { $0.intValue < $1.intValue } }
    if let months = rule.monthsOfTheYear, !months.isEmpty { result["months"] = months.sorted { $0.intValue < $1.intValue } }
    if let end = rule.recurrenceEnd {
        if let date = end.endDate { result["until"] = stamp(date) }
        else if end.occurrenceCount > 0 { result["count"] = end.occurrenceCount }
    }
    var advanced: [String: Any] = [:]
    for (key, values) in [("weeks_of_year",rule.weeksOfTheYear), ("days_of_year",rule.daysOfTheYear), ("set_positions",rule.setPositions)] {
        if let values, !values.isEmpty { advanced[key] = values }
    }
    result["calendar_identifier"] = rule.calendarIdentifier
    result["first_weekday"] = rule.firstDayOfTheWeek
    if !advanced.isEmpty { result["advanced"] = advanced }
    return result
}

func recurrenceRule(_ value: Any, anchor: Date?) throws -> EKRecurrenceRule? {
    if value is NSNull { return nil }
    guard let data = value as? [String: Any],
          Set(data.keys).isSubset(of: ["frequency","interval","weekdays","month_days","months","count","until","ordinal_weekdays"]),
          let frequency = recurrenceFrequencies[text(data["frequency"])] else {
        throw Failure(code:"invalid_field",message:"重复规则字段或频率无效。")
    }
    let interval = try alarmMinutes(data["interval"] ?? 1, minimum:1)
    guard interval <= 999, !(data["count"] != nil && data["until"] != nil) else {
        throw Failure(code:"invalid_field",message:"重复间隔无效，或同时设置了截止时间与次数。")
    }
    var end: EKRecurrenceEnd? = nil
    if let count = data["count"] {
        let number = try alarmMinutes(count, minimum:1)
        guard number <= 10000 else { throw Failure(code:"invalid_field",message:"重复次数超出范围。") }
        end = EKRecurrenceEnd(occurrenceCount:number)
    }
    if let until = data["until"] {
        let last = try date(until)
        if let anchor, last < anchor { throw Failure(code:"invalid_date",message:"重复截止时间早于首次日期。") }
        end = EKRecurrenceEnd(end:last)
    }
    var weekdays: [EKRecurrenceDayOfWeek]? = nil
    if let input = data["weekdays"] {
        guard frequency == .weekly, let days = input as? [String], !days.isEmpty,
              Set(days).count == days.count, days.allSatisfy({ recurrenceWeekdays.contains($0) }) else {
            throw Failure(code:"invalid_field",message:"星期列表仅用于每周重复，必须有效且不重复。")
        }
        weekdays = days.map { EKRecurrenceDayOfWeek(EKWeekday(rawValue:recurrenceWeekdays.firstIndex(of:$0)! + 1)!) }
    }
    if let input=data["ordinal_weekdays"] {
        guard [.monthly,.yearly].contains(frequency), data["weekdays"]==nil, data["month_days"]==nil, let values=input as? [[String:Any]], !values.isEmpty else { throw Failure(code:"invalid_field",message:"星期序号仅支持每月或每年。") }
        var seen=Set<String>()
        weekdays = try values.map { row in
            guard Set(row.keys)==["day","week"], let index=recurrenceWeekdays.firstIndex(of:text(row["day"])), let number=row["week"] as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(), number.doubleValue==Double(number.intValue), (1...5).contains(abs(number.intValue)), seen.insert("\(index):\(number.intValue)").inserted else { throw Failure(code:"invalid_field",message:"星期序号无效或重复。") }
            return EKRecurrenceDayOfWeek(dayOfTheWeek:EKWeekday(rawValue:index+1)!,weekNumber:number.intValue)
        }
    }
    func numbers(_ key: String, frequency required: EKRecurrenceFrequency, maximum: Int, negative: Bool) throws -> [NSNumber]? {
        guard let value = data[key] else { return nil }
        guard frequency == required, let values = value as? [Any], !values.isEmpty else {
            throw Failure(code:"invalid_field",message:"重复日期列表与频率不匹配。")
        }
        let result = try values.map { value -> NSNumber in
            guard let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(),
                  n.doubleValue.isFinite, n.doubleValue == n.doubleValue.rounded(),
                  abs(n.doubleValue) >= 1, abs(n.doubleValue) <= Double(maximum), negative || n.intValue > 0 else {
                throw Failure(code:"invalid_field",message:"重复日期列表无效。")
            }
            return n
        }
        guard Set(result).count == result.count else { throw Failure(code:"invalid_field",message:"重复日期列表不能包含重复值。") }
        return result
    }
    return EKRecurrenceRule(recurrenceWith:frequency, interval:interval, daysOfTheWeek:weekdays,
        daysOfTheMonth:try numbers("month_days",frequency:.monthly,maximum:31,negative:true),
        monthsOfTheYear:try numbers("months",frequency:.yearly,maximum:12,negative:false),
        weeksOfTheYear:nil, daysOfTheYear:nil, setPositions:nil, end:end)
}

func setRecurrence(_ item: EKCalendarItem, _ value: Any) throws {
    let anchor: Date?
    if let reminder = item as? EKReminder {
        anchor = reminder.dueDateComponents.flatMap { ($0.calendar ?? Calendar.current).date(from:$0) }
    } else { anchor = (item as? EKEvent)?.startDate }
    if !(value is NSNull) && anchor == nil { throw Failure(code:"invalid_date",message:"重复规则需要首次日期。") }
    let rule = try recurrenceRule(value,anchor:anchor)
    item.recurrenceRules = rule.map { [$0] } ?? []
}

func verifyRecurrence(_ item: EKCalendarItem, _ value: Any) throws {
    if value is NSNull {
        guard !item.hasRecurrenceRules else { throw Failure(code:"read_failed",message:"重复规则未清除。") }
        return
    }
    let expected = recurrencePayload(try recurrenceRule(value,anchor:nil)!)
    guard let rules = item.recurrenceRules, rules.count == 1 else { throw Failure(code:"read_failed",message:"重复规则数量不一致。") }
    let actual = recurrencePayload(rules[0])
    // Apple 可补齐隐含的星期和月份，仅对比本次明确指定的条件及结束条件。
    for key in Set((value as! [String: Any]).keys).union(["frequency","interval","count","until"]) {
        let left = expected[key] ?? NSNull(), right = actual[key] ?? NSNull()
        guard NSDictionary(dictionary:["v":left]).isEqual(to:["v":right]) else {
            throw Failure(code:"read_failed",message:"重复规则保存后不一致：\(key)")
        }
    }
}

func recurrenceSpan(_ request: [String: Any], item: EKCalendarItem, action: String) throws -> EKSpan {
    let scope = text(request["recurrence_scope"])
    let fields = request["fields"] as? [String: Any] ?? [:]
    if !scope.isEmpty && (action == "create" || (item is EKReminder ? scope != "series" : !["this","future"].contains(scope))) {
        throw Failure(code:"invalid_field",message:"重复操作范围无效。")
    }
    if item.hasRecurrenceRules && action != "create" {
        if item is EKEvent && !["this","future"].contains(scope) ||
            item is EKReminder && (action == "delete" || fields["recurrence"] != nil) && scope != "series" {
            throw Failure(code:"recurrence_scope_required",message:"请明确重复操作的范围。")
        }
    }
    if item is EKEvent && fields["recurrence"] != nil && scope == "this" {
        throw Failure(code:"recurrence_scope_required",message:"修改重复规则必须选择本次及以后。")
    }
    return scope == "future" ? .futureEvents : .thisEvent
}

func reminderLocation(_ reminder: EKReminder) -> Any {
    guard let alarm = (reminder.alarms ?? []).first(where: { $0.structuredLocation != nil }),
          let place = alarm.structuredLocation, let coordinate = place.geoLocation?.coordinate else { return NSNull() }
    return ["title": place.title ?? "", "address": "", "latitude": coordinate.latitude,
            "longitude": coordinate.longitude, "radius": place.radius,
            "trigger": alarm.proximity == .enter ? "arrive" : alarm.proximity == .leave ? "leave" : "none"] as [String: Any]
}

func locationAlarm(_ value: Any) throws -> EKAlarm? {
    if value is NSNull { return nil }
    guard let fields = value as? [String: Any],
          Set(fields.keys) == Set(["title", "address", "latitude", "longitude", "radius", "trigger"]),
          let latitude = fields["latitude"] as? NSNumber, let longitude = fields["longitude"] as? NSNumber,
          let radius = fields["radius"] as? NSNumber,
          [latitude, longitude, radius].allSatisfy({ CFGetTypeID($0) != CFBooleanGetTypeID() && $0.doubleValue.isFinite }),
          abs(latitude.doubleValue) <= 90, abs(longitude.doubleValue) <= 180,
          (100...10000).contains(radius.doubleValue), radius.doubleValue.rounded() == radius.doubleValue,
          !text(fields["title"]).isEmpty, ["arrive", "leave"].contains(text(fields["trigger"])) else {
        throw Failure(code: "invalid_field", message: "地点提醒需要有效的名称、坐标、100到10000米范围及到达或离开条件。")
    }
    let address = text(fields["address"])
    let place = EKStructuredLocation(title: text(fields["title"]) + (address.isEmpty ? "" : "（\(address)）"))
    place.geoLocation = CLLocation(latitude: latitude.doubleValue, longitude: longitude.doubleValue)
    place.radius = radius.doubleValue
    let alarm = EKAlarm()
    alarm.structuredLocation = place
    alarm.proximity = text(fields["trigger"]) == "arrive" ? .enter : .leave
    return alarm
}

func setReminderLocation(_ reminder: EKReminder, value: Any) throws {
    let alarm = try locationAlarm(value)
    // 只替换位置提醒，保留到期、绝对时间及其它非位置提醒。
    let retained = (reminder.alarms ?? []).filter { $0.structuredLocation == nil }
    reminder.alarms = retained + (alarm.map { [$0] } ?? [])
}

func verifyReminderLocation(_ reminder: EKReminder, value: Any) throws {
    let expected = try locationAlarm(value)
    let actual = (reminder.alarms ?? []).filter { $0.structuredLocation != nil }
    if expected == nil && actual.isEmpty { return }
    guard let expected, actual.count == 1, let saved = actual.first,
          saved.proximity == expected.proximity,
          let a = saved.structuredLocation, let b = expected.structuredLocation,
          let ac = a.geoLocation?.coordinate, let bc = b.geoLocation?.coordinate,
          abs(ac.latitude - bc.latitude) < 0.000001, abs(ac.longitude - bc.longitude) < 0.000001,
          abs(a.radius - b.radius) < 1, a.title == b.title else {
        throw Failure(code: "read_failed", message: "地点提醒保存后的地址、范围或触发条件不一致。")
    }
}

func searchLocations(_ request: [String: Any]) throws -> [String: Any] {
    let query = text(request["query"]).trimmingCharacters(in: .whitespacesAndNewlines)
    guard !query.isEmpty, query.count <= 300 else {
        throw Failure(code: "invalid_field", message: "请提供300字以内的城市和地点或地址。")
    }
    let searchRequest = MKLocalSearch.Request()
    searchRequest.naturalLanguageQuery = query
    let search = MKLocalSearch(request: searchRequest)
    var finished = false
    var failure: Error?
    var places: [[String: Any]] = []
    search.start { response, error in
        failure = error
        for item in response?.mapItems ?? [] {
            // 保持 macOS 14 最低部署版本；不请求或读取本机当前位置。
            let coordinate = item.placemark.coordinate
            places.append(["title": item.name ?? item.placemark.title ?? query,
                           "address": item.placemark.title ?? "", "latitude": coordinate.latitude,
                           "longitude": coordinate.longitude])
        }
        finished = true
    }
    let deadline = Date().addingTimeInterval(20)
    while !finished && Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.05)) }
    guard finished else {
        search.cancel()
        throw Failure(code: "read_failed", message: "Apple 地图地点查询超时，请稍后重试。")
    }
    if failure != nil { throw Failure(code: "read_failed", message: "Apple 地图暂时无法查询此地址，请补充完整地址或稍后重试。") }
    return ["locations": Array(places.prefix(10))]
}

func selectedCalendars(_ request: [String: Any], entity: EKEntityType) throws -> [EKCalendar] {
    let allowed = ids(request[entity == .event ? "calendar_ids" : "list_ids"])
    let all = store.calendars(for: entity)
    let selected = allowed.isEmpty ? all : all.filter { allowed.contains($0.calendarIdentifier) }
    if !allowed.isSubset(of: Set(all.map { $0.calendarIdentifier })) {
        throw Failure(code: "scope_missing", message: "配置的清单或日历已不存在，请重新选择同步范围。")
    }
    return selected
}

func fetchReminders(_ request: [String: Any]) throws -> [EKReminder] {
    let calendars = try selectedCalendars(request, entity: .reminder)
    if calendars.isEmpty { return [] }
    let semaphore = DispatchSemaphore(value: 0)
    var found: [EKReminder]? = nil
    store.fetchReminders(matching: store.predicateForReminders(in: calendars)) { values in
        found = values
        semaphore.signal()
    }
    if semaphore.wait(timeout: .now() + 30) == .timedOut || found == nil {
        throw Failure(code: "read_failed", message: "读取提醒事项失败，未返回完整快照。")
    }
    return found!
}

func fetchEvents(_ request: [String: Any]) throws -> [EKEvent] {
    let calendars = try selectedCalendars(request, entity: .event)
    if calendars.isEmpty { return [] }
    let start = try date(request["start"])
    let end = try date(request["end"])
    if end <= start || end.timeIntervalSince(start) > 366 * 86400 {
        throw Failure(code: "invalid_range", message: "日历查询范围必须递增且不超过 366 天。")
    }
    return store.events(matching: store.predicateForEvents(withStart: start, end: end, calendars: calendars))
}

func snapshot(_ request: [String: Any]) throws -> [String: Any] {
    let reminders = try fetchReminders(request).map { try reminderPayload($0) }
    let events = try fetchEvents(request).map { eventPayload($0) }
    let calendars = try selectedCalendars(request, entity: .reminder).map { calendarPayload($0, kind: "reminder") }
        + selectedCalendars(request, entity: .event).map { calendarPayload($0, kind: "event") }
    var result: [String: Any] = ["calendars": calendars, "items": reminders + events,
            "start": text(request["start"]), "end": text(request["end"])]
    do {
        let containers = try PersonalReminderContainers().snapshot(request)
        result.merge(containers) { _, new in new }
        var capabilities=containers["container_capabilities"] as? [String:Any] ?? [:]
        var extended=try eventCalendarRows(request)
        do { extended += try sectionRows(request);capabilities["sections"]=true }
        catch { capabilities["sections"]=false;capabilities["section_error"]=String(describing:error) }
        result["containers"] = (containers["containers"] as? [[String:Any]] ?? []) + extended
        result["container_capabilities"] = capabilities
    } catch {
        result["containers"] = []
        result["container_capabilities"] = ["groups": false, "icons": false, "error": String(describing: error)]
    }
    return result
}

func target(_ request: [String: Any], kind: String, id: String) throws -> EKCalendarItem {
    if kind == "reminder" {
        if let result = try fetchReminders(request).first(where: { $0.calendarItemIdentifier == id }) { return result }
    } else if kind == "event" {
        if let result = try fetchEvents(request).first(where: { eventID($0) == id }) { return result }
    }
    throw Failure(code: "not_found", message: "事项已删除、移动或不在查询范围；请重新查询。")
}

func payload(_ item: EKCalendarItem) throws -> [String: Any] {
    if let reminder = item as? EKReminder { return try reminderPayload(reminder) }
    return eventPayload(item as! EKEvent)
}

func setFields(_ item: EKCalendarItem, fields: [String: Any]) throws {
    let reminderFields: Set<String> = ["title", "notes", "url", "calendar_id", "due", "completed", "priority", "location", "recurrence"]
    let eventFields: Set<String> = ["title", "notes", "url", "calendar_id", "start", "end", "all_day", "location", "alarm_minutes_before", "recurrence", "travel_time_minutes"]
    let allowed = item is EKReminder ? reminderFields : eventFields
    if !Set(fields.keys).isSubset(of: allowed) { throw Failure(code: "unsupported_field", message: "包含第一版未支持的字段。") }
    if let title = fields["title"] {
        if text(title).trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            throw Failure(code: "invalid_title", message: "标题不能为空。")
        }
        item.title = text(title)
    }
    if let notes = fields["notes"] { item.notes = text(notes) }
    if let url = fields["url"] {
        if text(url).isEmpty { item.url = nil }
        else if let parsed = URL(string: text(url)), parsed.scheme != nil { item.url = parsed }
        else { throw Failure(code: "invalid_url", message: "URL 必须包含协议。") }
    }
    if let reminder = item as? EKReminder {
        if let completed = fields["completed"] {
            guard let value = completed as? Bool else { throw Failure(code: "invalid_field", message: "completed 必须为布尔值。") }
            reminder.isCompleted = value
        }
        if let priority = fields["priority"] {
            guard let value = priority as? Int, (0...9).contains(value) else {
                throw Failure(code: "invalid_field", message: "priority 必须是 0 到 9 的整数。")
            }
            reminder.priority = value
        }
        if let due = fields["due"] {
            let oldDue = reminder.dueDateComponents.flatMap { ($0.calendar ?? Calendar.current).date(from: $0) }
            let existingAlarms = reminder.alarms ?? []
            let input = text(due)
            if input.isEmpty { reminder.dueDateComponents = nil }
            else if input.count == 10 {
                let parser = DateFormatter()
                parser.dateFormat = "yyyy-MM-dd"
                parser.isLenient = false
                guard let parsed = parser.date(from: input), parser.string(from: parsed) == input else {
                    throw Failure(code: "invalid_date", message: "无效的日期。")
                }
                var components = Calendar.current.dateComponents([.year, .month, .day], from: parsed)
                components.calendar = Calendar.current
                reminder.dueDateComponents = components
            } else {
                var components = Calendar.current.dateComponents([.year, .month, .day, .hour, .minute, .second], from: try date(due))
                components.calendar = Calendar.current
                components.timeZone = TimeZone.current
                reminder.dueDateComponents = components
            }
            // 到期日期本身不等于时间通知；更新该时刻的通知时保留位置和其它通知。
            var alarms = existingAlarms.filter { alarm in
                guard alarm.structuredLocation == nil, let absolute = alarm.absoluteDate, let oldDue else { return true }
                return abs(absolute.timeIntervalSince(oldDue)) >= 1
            }
            if let components = reminder.dueDateComponents, components.hour != nil,
               let dueDate = (components.calendar ?? Calendar.current).date(from: components),
               !alarms.contains(where: { $0.structuredLocation == nil && $0.absoluteDate == dueDate }) {
                alarms.append(EKAlarm(absoluteDate: dueDate))
            }
            reminder.alarms = alarms
        }
    } else if let event = item as? EKEvent {
        if let value = fields["travel_time_minutes"] { try setTravel(event,value) }
        if let start = fields["start"] { event.startDate = try date(start) }
        if let end = fields["end"] { event.endDate = try date(end) }
        if let allDay = fields["all_day"] {
            guard let value = allDay as? Bool else { throw Failure(code: "invalid_field", message: "all_day 必须为布尔值。") }
            event.isAllDay = value
        }
        if let location = fields["location"] { event.location = text(location) }
        if event.startDate == nil || event.endDate == nil || event.endDate <= event.startDate {
            throw Failure(code: "invalid_date", message: "日程结束时间必须晚于开始时间。")
        }
    }
    if let rule = fields["recurrence"] { try setRecurrence(item, rule) }
    if let value = fields["location"], let reminder = item as? EKReminder {
        try setReminderLocation(reminder, value: value)
    }
    // EventKit 在切换全天属性时重写 alarms，因此显式提醒必须最后赋值。
    if let value = fields["alarm_minutes_before"], let event = item as? EKEvent {
        guard let values = value as? [Any] else {
            throw Failure(code: "invalid_field", message: "日程提醒必须是分钟数列表；空列表清除。")
        }
        let minutes = try values.map { try alarmMinutes($0) }
        guard Set(minutes).count == minutes.count else {
            throw Failure(code: "invalid_field", message: "日程提醒时间不能重复。")
        }
        if minutes.count > 1 && event.calendar?.source?.sourceType == .exchange {
            throw Failure(code: "invalid_field", message: "Exchange 日历只支持一个提醒。")
        }
        event.alarms = minutes.map { EKAlarm(relativeOffset: -Double($0) * 60) }
    }
}

func comparableAppleState(_ value: [String: Any]) -> [String: Any] {
    // Diagnostic NSError descriptions may contain process-specific addresses.
    // Keep the actual section value (including unknown/null) in the baseline.
    var state = value
    state.removeValue(forKey: "section_read_error")
    return state
}

func write(_ request: [String: Any], action: String) throws -> [String: Any] {
    let kind = text(request["kind"])
    if !["reminder", "event"].contains(kind) { throw Failure(code: "invalid_kind", message: "无效的事项类型。") }
    let fields = request["fields"] as? [String: Any] ?? [:]
    let item: EKCalendarItem
    if action == "create" {
        if text(fields["title"]).isEmpty || text(fields["calendar_id"]).isEmpty {
            throw Failure(code: "invalid_fields", message: "创建必须提供标题和目标清单或日历。")
        }
        item = kind == "reminder" ? EKReminder(eventStore: store) : EKEvent(eventStore: store)
    } else {
        item = try target(request, kind: kind, id: text(request["id"]))
        let expected = request["expected"] as? [String: Any] ?? [:]
        if try expected.isEmpty || !NSDictionary(dictionary: comparableAppleState(payload(item))).isEqual(to: comparableAppleState(expected)) {
            throw Failure(code: "conflict", message: "事项已被修改，操作未执行。请重新查询并确认。")
        }
    }
    let span = try recurrenceSpan(request, item:item, action:action)
    if action != "delete" {
        let calendarID = text(fields["calendar_id"])
        if !calendarID.isEmpty {
            let entity = kind == "event" ? EKEntityType.event : .reminder
            guard let calendar = try selectedCalendars(request, entity: entity).first(where: { $0.calendarIdentifier == calendarID }), calendar.allowsContentModifications else {
                throw Failure(code: "read_only", message: "目标清单或日历不可写或不在同步范围。")
            }
            item.calendar = calendar
        }
    }
    if !item.calendar.allowsContentModifications { throw Failure(code: "read_only", message: "事项所在日历或清单只读。") }
    if action=="delete", let reminder = item as? EKReminder { try validateReminderRelations(request,reminder,fields:fields) }
    if action == "delete" {
        if let reminder = item as? EKReminder { try store.remove(reminder, commit: true) }
        else { try store.remove(item as! EKEvent, span: span, commit: true) }
        return ["deleted": true, "id": text(request["id"])]
    }
    if kind == "reminder", fields["due"] != nil, text(fields["due"]).isEmpty,
       item.hasRecurrenceRules, !(fields["recurrence"] is NSNull) {
        throw Failure(code:"invalid_date",message:"取消到期日期前须同时取消重复规则。")
    }
    let early = fields["early_reminder_minutes"]
    if let tags = fields["tags"] {
        guard kind == "reminder" else { throw Failure(code:"invalid_field",message:"标签仅适用于提醒事项。") }
        _ = try reminderTagNames(tags)
    }
    if (early != nil || fields["flagged"] != nil) && kind != "reminder" {
        throw Failure(code: "invalid_field", message: "提前事项提醒仅适用于提醒事项。")
    }
    if let flag = fields["flagged"] {
        guard let number = flag as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else {
            throw Failure(code:"invalid_field",message:"旗标必须是布尔值。")
        }
    }
    try setFields(item, fields: fields.filter { !["early_reminder_minutes","flagged","tags","parent_id","section_id","urgent","attachments"].contains($0.key) })
    if let reminder=item as? EKReminder { try validateReminderRelations(request,reminder,fields:fields) }
    if (early != nil || fields["flagged"] != nil || fields["tags"] != nil || ["parent_id","section_id","urgent","attachments"].contains(where:{fields[$0] != nil})), let reminder = item as? EKReminder {
        return try PersonalReminderAlerts().write(reminder, fields: fields, action: action, early: early)
    }
    if let reminder = item as? EKReminder { try store.save(reminder, commit: true) }
    else { try store.save(item as! EKEvent, span: span, commit: true) }
    if (fields["location"] != nil || fields["recurrence"] != nil), let reminder = item as? EKReminder {
        let identifier = reminder.calendarItemIdentifier
        store.reset(); membershipCache.removeAll()
        do {
            guard let saved = store.calendarItem(withIdentifier: identifier) as? EKReminder else {
                throw Failure(code: "read_failed", message: "事项已提交，暂时无法重新读取。")
            }
            if let value = fields["location"] { try verifyReminderLocation(saved, value:value) }
            if let rule = fields["recurrence"] { try verifyRecurrence(saved, rule) }
            return ["item": try reminderPayload(saved)]
        } catch { throw Failure(code: "unknown_write", message: "事项已提交，地点或重复规则保存结果需核实：\(error)") }
    }
    if (fields["alarm_minutes_before"] != nil || fields["recurrence"] != nil || fields["travel_time_minutes"] != nil), let event = item as? EKEvent {
        let identifier = eventID(event)
        let savedScope: [String: Any] = ["calendar_ids":[event.calendar.calendarIdentifier],
            "start":stamp(event.startDate.addingTimeInterval(-86400)),
            "end":stamp(event.startDate.addingTimeInterval(86400))]
        store.reset(); membershipCache.removeAll()
        do {
            let saved = try target(savedScope, kind:"event", id:identifier) as! EKEvent
            let actual = eventPayload(saved)
            if let minutes=fields["travel_time_minutes"] as? Int, travelTime(saved) != Double(minutes) { throw Failure(code:"read_failed",message:"出行时间未保存。") }
            if let offsets = fields["alarm_minutes_before"] as? [Int] {
                guard (actual["alarm_minutes_before"] as? [Int])?.sorted() == offsets.sorted(),
                      (saved.alarms ?? []).count == offsets.count else {
                    throw Failure(code: "read_failed", message: "日历服务未保留完整提醒列表。")
                }
            }
            if let rule = fields["recurrence"] { try verifyRecurrence(saved, rule) }
            return ["item": actual]
        } catch { throw Failure(code: "unknown_write", message: "日程已提交，提醒保存结果需核实：\(error)") }
    }
    return ["item": try payload(item)]
}

func reminderTagNames(_ value: Any) throws -> [String] {
    guard let names = value as? [String], names.allSatisfy({
        !$0.isEmpty && !$0.contains("#") && $0.rangeOfCharacter(from: .whitespacesAndNewlines) == nil
    }), Set(names.map { $0.lowercased() }).count == names.count else {
        throw Failure(code:"invalid_field",message:"标签须为不带#、不含空白且不重复的名称列表。")
    }
    return names.sorted()
}

// 原生标签、提前提醒和旗标共用 ReminderKit 保存请求。
// 事项字段、标签、旗标和提前提醒共用 REMSaveRequest；地点与重复规则随后保存，失败按未知写入处理。
struct PersonalReminderAlerts {
    let native: PersonalReminderContainers
    init() throws { native = try PersonalReminderContainers() }

    func reminder(_ id: String) throws -> NSObject {
        guard UUID(uuidString: id) != nil,
              let cls = NSClassFromString("REMObjectID") as? NSObject.Type,
              let url = URL(string: "x-apple-reminderkit://REMCDReminder/\(id)"),
              let objectID = cls.perform(NSSelectorFromString("objectIDWithURL:"), with: url as NSURL)?.takeUnretainedValue() as? NSObject,
              let value = try remFetch(native.remStore, "fetchReminderWithObjectID:error:", objectID) as? NSObject,
              remID(value).caseInsensitiveCompare(id) == .orderedSame else {
            throw Failure(code: "read_failed", message: "无法读取事项的原生提前提醒。")
        }
        return value
    }

    // fetchReminder 返回序列化的提前提醒；context 的 fetched* 缓存并不保证已填充。
    // 只解析原生对象提供的数据，不读取 Reminders 私有数据库。
    func alerts(_ reminder: NSObject) throws -> [[String: Any]] {
        guard reminder.responds(to: NSSelectorFromString("dueDateDeltaAlertsData")) else {
            throw Failure(code: "unsupported_private_feature", message: "系统未提供提前提醒读取接口。")
        }
        guard let data = remValue(reminder, "dueDateDeltaAlertsData") as? Data else { return [] }
        guard let root = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let rows = root["dueDateDeltaAlerts"] as? [[String: Any]],
              rows.allSatisfy({ $0["identifier"] is String && $0["dueDateDeltaUnit"] is Int && $0["dueDateDeltaCount"] is Int }) else {
            throw Failure(code: "read_failed", message: "无法识别当前系统的提前提醒数据。")
        }
        return rows
    }

    func tags(_ reminder: NSObject) throws -> [String] {
        let context = try remCall(reminder, "hashtagContext")
        let value = try remCall(context, "hashtags")
        let rows: [NSObject]
        if let array = value as? [NSObject] { rows = array }
        else if let set = value as? NSSet, let array = set.allObjects as? [NSObject] { rows = array }
        else { throw Failure(code:"read_failed",message:"无法识别原生标签数据。") }
        return try rows.map { tag in
            guard let name = remValue(tag, "name") as? String, !name.isEmpty else {
                throw Failure(code:"read_failed",message:"无法读取原生标签名称。")
            }
            return name
        }.sorted()
    }

    // 复用旧 native_helper 的 hashtagContext 和 addHashtagWithType:name:。
    func replaceTags(_ change: NSObject, _ names: [String]) throws {
        let context = try remCall(change, "hashtagContext")
        let selector = NSSelectorFromString("addHashtagWithType:name:")
        guard let method = class_getInstanceMethod(type(of: context), selector),
              context.responds(to: NSSelectorFromString("removeAllHashtags")) else {
            throw Failure(code:"unsupported_private_feature",message:"系统未提供标签写入接口。")
        }
        // 系统会替换不允许的字符；拒绝这种隐式改名，在保存前让用户更正。
        for name in names {
            let normalized = try remCall(context, "nameWithDisallowedCharactersReplaced:", [name as NSString])
            guard normalized as? String == name else {
                throw Failure(code:"invalid_field",message:"标签包含系统不支持的字符。")
            }
        }
        _ = context.perform(NSSelectorFromString("removeAllHashtags"))
        let add = unsafeBitCast(method_getImplementation(method),
            to: (@convention(c) (AnyObject, Selector, Int, AnyObject) -> AnyObject?).self)
        for name in names {
            guard add(context, selector, 1, name as NSString) != nil else {
                throw Failure(code:"invalid_field",message:"无法添加原生标签。")
            }
        }
    }

    func state(_ id: String) throws -> [String: Any] {
        let object = try reminder(id)
        guard let flag = remValue(remObject(object, "flaggedContext"), "flagged") as? NSNumber else {
            throw Failure(code:"unsupported_private_feature",message:"系统未提供旗标读取接口。")
        }
        let extra = try extendedReminderState(object)
        let names = try tags(object)
        let rows = try alerts(object)
        guard rows.count <= 1 else {
            throw Failure(code: "read_failed", message: "事项包含多个原生提前提醒，需要先在 Apple 中核实。")
        }
        guard let alert = rows.first else {
            return extra.merging(["tags":names, "flagged":flag.boolValue, "early_reminder_minutes": NSNull(), "early_reminder": NSNull()]){_,n in n}
        }
        let unit = alert["dueDateDeltaUnit"] as! Int
        let count = alert["dueDateDeltaCount"] as! Int
        let factors = [0: 1, 1: 60, 2: 1440, 3: 10080]
        let minutes = factors[unit].map { -count * $0 }
        return extra.merging(["tags":names, "flagged":flag.boolValue, "early_reminder_minutes": minutes as Any? ?? NSNull(), "early_reminder": alert]){_,n in n}
    }

    static func interval(_ minutes: Int) throws -> NSObject {
        guard let cls = NSClassFromString("REMDueDateDeltaInterval") as? NSObject.Type,
              let object = cls.perform(NSSelectorFromString("alloc"))?.takeUnretainedValue(),
              let method = class_getInstanceMethod(cls, NSSelectorFromString("initWithUnitInteger:count:")) else {
            throw Failure(code: "unsupported_private_feature", message: "系统未提供提前提醒间隔接口。")
        }
        let selector = NSSelectorFromString("initWithUnitInteger:count:")
        let fn = unsafeBitCast(method_getImplementation(method),
            to: (@convention(c) (AnyObject, Selector, Int, Int) -> AnyObject).self)
        // 0=分钟，2=日；整日按日计算，跨夏令时仍保持当地相同时刻。
        return fn(object, selector, minutes % 1440 == 0 ? 2 : 0,
                  -(minutes % 1440 == 0 ? minutes / 1440 : minutes)) as! NSObject
    }

    func write(_ item: EKReminder, fields: [String: Any], action: String, early: Any?) throws -> [String: Any] {
        let minutes = early == nil || early is NSNull ? nil : try alarmMinutes(early!, minimum: 1)
        if minutes != nil && item.dueDateComponents?.hour == nil {
            throw Failure(code: "invalid_date", message: "提前提醒需要明确的到期日期、时刻和时区。")
        }
        let request = try native.saveRequest()
        let change: NSObject
        if action == "create" {
            let listChange = try remCall(request, "updateList:", [try native.list(item.calendar.calendarIdentifier)])
            change = try remCall(request, "addReminderWithTitle:toListChangeItem:", [item.title as NSString, listChange])
        } else {
            change = try remCall(request, "updateReminder:", [try reminder(item.calendarItemIdentifier)])
        }
        // 只修改本次明确提交的字段；保留位置提醒、附件和其它原生属性。
        for (field, key) in [("title", "titleAsString"), ("notes", "notesAsString"),
                             ("url", "icsUrl"), ("completed", "completed"), ("priority", "priority")] where fields[field] != nil {
            let value: Any?
            switch field {
            case "title": value = item.title
            case "notes": value = item.notes
            case "url": value = item.url
            case "completed": value = item.isCompleted
            default: value = item.priority
            }
            let setter = "set" + key.prefix(1).uppercased() + key.dropFirst() + ":"
            guard change.responds(to: NSSelectorFromString(setter)) else {
                throw Failure(code: "unsupported_private_feature", message: "系统不支持事项字段：\(key)")
            }
            change.setValue(value, forKey: key)
        }
        if let value = fields["tags"] { try replaceTags(change, reminderTagNames(value)) }
        if let flag = fields["flagged"] {
            let context = try remCall(change, "flaggedContext")
            guard context.responds(to:NSSelectorFromString("setFlagged:")) else {
                throw Failure(code:"unsupported_private_feature",message:"系统未提供旗标写入接口。")
            }
            context.setValue(flag, forKey:"flagged")
        }
        if action != "create" && fields["calendar_id"] != nil {
            try remSet(change, "setListID:", remObject(try native.list(item.calendar.calendarIdentifier), "remObjectID"))
        }
        if fields["due"] != nil {
            try remSet(change, "setDueDateComponentsWithAlarmsIfNeeded:", item.dueDateComponents as NSDateComponents?)
        }
        if early != nil {
            let context = try remCall(change, "dueDateDeltaAlertContext")
            if action != "create" {
                let previous = try alerts(reminder(item.calendarItemIdentifier))
                let identifiers = try previous.map { row -> NSUUID in
                    guard let id = NSUUID(uuidString: row["identifier"] as! String) else {
                        throw Failure(code: "read_failed", message: "提前提醒编号无效。")
                    }
                    return id
                }
                if !identifiers.isEmpty {
                    try remSet(context, "removeDueDateDeltaAlertsWithIdentifiers:", NSSet(array: identifiers))
                }
            }
            if let minutes {
                _ = try remCall(context, "addDueDateDeltaAlertWithDueDateDelta:", [try Self.interval(minutes)])
            }
        }
        try setExtendedReminder(native,request,change,fields:fields,old:action=="create" ? nil : try reminder(item.calendarItemIdentifier))
        let id = remID(change)
        if minutes != nil {
            guard (remValue(change, "dueDateDeltaAlertsToUpsert") as? [NSObject])?.count == 1 else {
                throw Failure(code: "invalid_field", message: "提前提醒未加入保存请求。")
            }
        }
        try native.save(request)
        // 保存后的读取失败属于未知写入，不允许自动重试。
        do {
            store.reset(); membershipCache.removeAll()
            guard let saved = store.calendarItem(withIdentifier: id) as? EKReminder else {
                throw Failure(code: "read_failed", message: "事项已提交，暂时无法重新读取。")
            }
            // 旗标和提前提醒由 ReminderKit 保存；明确修改地点或重复规则时随后通过 EventKit 保存。
            // 此阶段失败视为未知写入，不允许自动重放创建。
            if fields["location"] != nil || fields["recurrence"] != nil {
                if let value = fields["location"] { try setReminderLocation(saved, value:value) }
                if let rule = fields["recurrence"] { try setRecurrence(saved, rule) }
                try store.save(saved, commit:true)
                store.reset(); membershipCache.removeAll()
            }
            guard let saved = store.calendarItem(withIdentifier: id) as? EKReminder else {
                throw Failure(code: "read_failed", message: "事项已提交，暂时无法重新读取。")
            }
            if let value = fields["location"] { try verifyReminderLocation(saved, value: value) }
            if let rule = fields["recurrence"] { try verifyRecurrence(saved, rule) }
            let result = try reminderPayload(saved)
            for key in ["parent_id","section_id","urgent"] where fields[key] != nil {
                guard NSDictionary(dictionary:["v":result[key] ?? NSNull()]).isEqual(to:["v":fields[key]!]) else { throw Failure(code:"read_failed",message:"原生字段保存后不一致：\(key)") }
            }
            if let specs=fields["attachments"] as? [[String:Any]], (result["attachments"] as? [[String:Any]])?.count != specs.count { throw Failure(code:"read_failed",message:"附件数量保存后不一致。") }
            if let value = fields["tags"], result["tags"] as? [String] != (try reminderTagNames(value)) {
                throw Failure(code:"read_failed",message:"标签保存后的值不一致。")
            }
            if let flag = fields["flagged"] as? Bool, result["flagged"] as? Bool != flag {
                throw Failure(code:"read_failed",message:"旗标保存后的值不一致。")
            }
            if let minutes {
                guard (result["early_reminder_minutes"] as? Int) == minutes else {
                    throw Failure(code: "read_failed", message: "提前提醒保存后的值不一致。")
                }
            } else if early != nil && !(result["early_reminder"] is NSNull) {
                throw Failure(code: "read_failed", message: "提前提醒未成功清除。")
            }
            return ["item": result]
        } catch { throw Failure(code: "unknown_write", message: "事项已提交，保存结果需核实：\(error)") }
    }
}

// 群组、图标复用 Telos 原生 ReminderKit 接口；普通清单字段仍使用 EventKit。
// 不读取内部 SQLite、不按名称猜测对象、不操控界面。
func remValue(_ object: NSObject?, _ name: String) -> Any? {
    guard let object, object.responds(to: NSSelectorFromString(name)) else { return nil }
    return object.value(forKey: name)
}
func remObject(_ object: NSObject?, _ name: String) -> NSObject? { remValue(object, name) as? NSObject }
func remID(_ object: NSObject?) -> String {
    guard let raw = remObject(object, "remObjectID") ?? remObject(object, "objectID") ?? object else { return "" }
    return (remValue(raw, "uuid") as? NSUUID)?.uuidString ?? ""
}
func remCall(_ object: NSObject, _ name: String, _ args: [AnyObject] = []) throws -> NSObject {
    let selector = NSSelectorFromString(name)
    guard object.responds(to: selector) else { throw Failure(code: "unsupported_private_feature", message: "系统不支持原生接口：\(name)") }
    let value: AnyObject?
    if args.count == 0 { value = object.perform(selector)?.takeUnretainedValue() }
    else if args.count == 1 { value = object.perform(selector, with: args[0])?.takeUnretainedValue() }
    else { value = object.perform(selector, with: args[0], with: args[1])?.takeUnretainedValue() }
    guard let result = value as? NSObject else { throw Failure(code: "unsupported_private_feature", message: "原生接口没有返回对象：\(name)") }
    return result
}
func remSet(_ object: NSObject, _ name: String, _ value: AnyObject?) throws {
    let selector = NSSelectorFromString(name)
    guard object.responds(to: selector) else { throw Failure(code: "unsupported_private_feature", message: "系统不支持原生接口：\(name)") }
    _ = object.perform(selector, with: value)
}
func remFetch(_ object: NSObject, _ name: String, _ argument: AnyObject? = nil) throws -> AnyObject {
    let selector = NSSelectorFromString(name)
    guard let method = class_getInstanceMethod(type(of: object), selector) else {
        throw Failure(code: "unsupported_private_feature", message: "系统不支持读取接口：\(name)")
    }
    var error: NSError?
    let result: AnyObject?
    if let argument {
        let fn = unsafeBitCast(method_getImplementation(method), to: (@convention(c) (AnyObject, Selector, AnyObject, UnsafeMutablePointer<NSError?>) -> AnyObject?).self)
        result = fn(object, selector, argument, &error)
    } else {
        let fn = unsafeBitCast(method_getImplementation(method), to: (@convention(c) (AnyObject, Selector, UnsafeMutablePointer<NSError?>) -> AnyObject?).self)
        result = fn(object, selector, &error)
    }
    guard let result, error == nil else { throw Failure(code: "read_failed", message: error?.localizedDescription ?? "原生读取失败：\(name)") }
    return result
}
func colorHex(_ color: NSColor) -> String {
    guard let rgb = color.usingColorSpace(.sRGB) else { return "" }
    return String(format: "#%02X%02X%02X", Int(round(rgb.redComponent * 255)), Int(round(rgb.greenComponent * 255)), Int(round(rgb.blueComponent * 255)))
}
let reminderColors: [String: NSColor] = ["red": .systemRed, "orange": .systemOrange, "yellow": .systemYellow,
    "green": .systemGreen, "blue": .systemBlue, "purple": .systemPurple, "gray": .systemGray,
    "brown": .systemBrown, "pink": .systemPink]
let reminderSymbols = ["default"] + ["bookmarks": 2, "celebration": 2, "concept": 3, "education": 5,
    "finance": 3, "health": 2, "lifestyle": 2, "location": 3, "media": 5, "nature": 2,
    "people": 3, "pet": 3, "shopping": 4, "sport": 6, "symbol": 7, "transport": 4,
    "weather": 5, "work": 5].sorted { $0.key < $1.key }.flatMap { key, count in (1...count).map { "\(key)\($0)" } }
func parseColor(_ input: String) throws -> NSColor {
    if let color = reminderColors[input.lowercased()] { return color }
    guard input.range(of: "^#[0-9a-fA-F]{6}$", options: .regularExpression) != nil, let rgb = Int(input.dropFirst(), radix: 16) else {
        throw Failure(code: "invalid_color", message: "颜色须为 #RRGGBB 或查询返回的颜色名。")
    }
    return NSColor(srgbRed: Double((rgb >> 16) & 255) / 255, green: Double((rgb >> 8) & 255) / 255, blue: Double(rgb & 255) / 255, alpha: 1)
}

final class PersonalReminderContainers {
    let remStore: NSObject
    let accountsView: NSObject
    let listsView: NSObject
    init() throws {
        guard let bundle = Bundle(path: "/System/Library/PrivateFrameworks/ReminderKit.framework"), bundle.load(),
              let cls = NSClassFromString("REMStore") as? NSObject.Type else {
            throw Failure(code: "unsupported_private_feature", message: "当前 macOS 不支持 ReminderKit 群组与图标接口。")
        }
        let nativeStore = cls.init()
        remStore = nativeStore
        func view(_ name: String) throws -> NSObject {
            guard let cls = NSClassFromString(name) as? NSObject.Type,
                  let allocated = cls.perform(NSSelectorFromString("alloc"))?.takeUnretainedValue() as? NSObject else {
                throw Failure(code: "unsupported_private_feature", message: "缺少原生接口：\(name)")
            }
            return try remCall(allocated, "initWithStore:", [nativeStore])
        }
        accountsView = try view("REMAccountsDataView")
        listsView = try view("REMListsDataView")
    }
    func accounts() throws -> [NSObject] {
        guard let result = try remFetch(accountsView, "fetchAllAccountsWithError:") as? [NSObject] else {
            throw Failure(code: "read_failed", message: "无法完整读取提醒事项账户。")
        }
        return result
    }
    func list(_ id: String) throws -> NSObject {
        guard let cls = NSClassFromString("REMObjectID") as? NSObject.Type,
              let url = URL(string: "x-apple-reminderkit://REMCDList/\(id)"),
              let objectID = cls.perform(NSSelectorFromString("objectIDWithURL:"), with: url as NSURL)?.takeUnretainedValue() as? NSObject else {
            throw Failure(code: "not_found", message: "无效的清单或群组编号。")
        }
        do {
            guard let value = try remFetch(remStore, "fetchListWithObjectID:error:", objectID) as? NSObject,
                  remID(value).caseInsensitiveCompare(id) == .orderedSame else { throw Failure(code: "not_found", message: "清单或群组已不存在。") }
            return value
        } catch { throw Failure(code: "not_found", message: "清单或群组已不存在，请重新查询。") }
    }
    func isGroup(_ list: NSObject) throws -> Bool {
        guard let flag = remValue(list, "isGroup") as? NSNumber else {
            throw Failure(code: "unsupported_private_feature", message: "无法核实对象是否为群组。")
        }
        return flag.boolValue
    }
    func allGroups() throws -> [NSObject] {
        var result: [NSObject] = []
        for account in try accounts() {
            guard let lists = try remFetch(listsView, "fetchListsInAccount:error:", account) as? [NSObject] else {
                throw Failure(code: "read_failed", message: "无法完整读取群组。")
            }
            for list in lists where try isGroup(list) { result.append(list) }
        }
        return result
    }
    func snapshot(_ request: [String: Any]) throws -> [String: Any] {
        let calendars = try selectedCalendars(request, entity: .reminder)
        let reminders = try fetchReminders(request)
        var lists: [[String: Any]] = []
        var sources: [String: EKSource] = [:]
        for calendar in calendars {
            let object = try list(calendar.calendarIdentifier)
            let accountID = remID(remObject(object, "account"))
            sources[accountID] = calendar.source
            let appearance = remObject(object, "appearanceContext")
            let badge = remObject(appearance, "badge")
            var emoji = text(remValue(badge, "emoji"))
            let emblem = text(remValue(appearance, "badgeEmblem"))
            if emoji.isEmpty, let data = emblem.data(using: .utf8), let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                emoji = text(json["Emoji"])
            }
            lists.append(["id": calendar.calendarIdentifier, "kind": "list", "title": calendar.title,
                "writable": calendar.allowsContentModifications, "account_id": accountID,
                "group_id": remID(remObject(object, "parentList")), "color": colorHex(calendar.color),
                "emoji": emoji, "symbol": emoji.isEmpty ? (emblem.isEmpty ? "default" : emblem) : "",
                "item_count": reminders.filter { $0.calendar.calendarIdentifier == calendar.calendarIdentifier }.count])
        }
        let restricted = !ids(request["list_ids"]).isEmpty
        var groups: [[String: Any]] = []
        for group in try allGroups() {
            let id = remID(group)
            let accountID = remID(remObject(group, "account"))
            let children = lists.filter { text($0["group_id"]) == id }.map { text($0["id"]) }.sorted()
            if restricted && children.isEmpty { continue }
            groups.append(["id": id, "kind": "group", "title": text(remValue(group, "displayName")),
                "account_id": accountID, "writable": !restricted && lists.contains { text($0["account_id"]) == accountID && $0["writable"] as? Bool == true },
                "child_list_ids": children])
        }
        var accountRows: [[String: Any]] = []
        for account in try accounts() {
            let id = remID(account)
            if restricted && sources[id] == nil { continue }
            accountRows.append(["id": id, "title": text(remValue(account, "displayName")),
                "writable": !restricted && lists.contains { text($0["account_id"]) == id && $0["writable"] as? Bool == true }])
        }
        let defaultAccountID = lists.first { text($0["id"]) == store.defaultCalendarForNewReminders()?.calendarIdentifier }?["account_id"] ?? ""
        return ["containers": lists + groups, "container_capabilities": ["groups": true, "icons": true,
            "colors": reminderColors.mapValues { colorHex($0) }, "symbols": reminderSymbols, "accounts": accountRows,
            "default_account_id": defaultAccountID, "default_event_account_id":store.defaultCalendarForNewEvents?.source.sourceIdentifier ?? "", "restricted_scope": restricted]]
    }
    func detail(_ request: [String: Any]) throws -> [String: Any] {
        let snapshot = try snapshot(request)
        let rows = snapshot["containers"] as! [[String: Any]]
        guard let item = rows.first(where: { text($0["id"]) == text(request["id"]) && text($0["kind"]) == text(request["kind"]) }) else {
            throw Failure(code: "not_found", message: "清单或群组不存在，或不在同步范围。")
        }
        return item
    }
    func saveRequest() throws -> NSObject {
        guard let cls = NSClassFromString("REMSaveRequest") as? NSObject.Type,
              let allocated = cls.perform(NSSelectorFromString("alloc"))?.takeUnretainedValue() as? NSObject else {
            throw Failure(code: "unsupported_private_feature", message: "缺少原生保存接口。")
        }
        return try remCall(allocated, "initWithStore:", [remStore])
    }
    func save(_ request: NSObject) throws {
        let selector = NSSelectorFromString("saveSynchronouslyWithError:")
        guard let method = class_getInstanceMethod(type(of: request), selector) else {
            throw Failure(code: "unsupported_private_feature", message: "缺少原生保存接口。")
        }
        var error: NSError?
        let fn = unsafeBitCast(method_getImplementation(method), to: (@convention(c) (AnyObject, Selector, UnsafeMutablePointer<NSError?>) -> Bool).self)
        guard fn(request, selector, &error) else { throw Failure(code: "native_error", message: error?.localizedDescription ?? "原生保存结果无法确认。") }
    }
    func setGroup(_ change: NSObject, _ group: NSObject?, _ account: NSObject) throws {
        try remSet(change, "setParentOwnerID:", remObject(account, "remObjectID"))
        try remSet(change, "setParentSubContainerID:", remObject(group, "remObjectID"))
    }
    func setIcon(_ change: NSObject, _ fields: [String: Any]) throws {
        guard fields["emoji"] != nil || fields["symbol"] != nil else { return }
        let context = try remCall(change, "appearanceContext")
        guard let cls = NSClassFromString("REMListBadge") as? NSObject.Type,
              let allocated = cls.perform(NSSelectorFromString("alloc"))?.takeUnretainedValue() as? NSObject else {
            throw Failure(code: "unsupported_private_feature", message: "缺少原生图标接口。")
        }
        let badge = try remCall(allocated, fields["emoji"] != nil ? "initWithEmoji:" : "initWithEmblem:",
                                [(fields["emoji"] ?? fields["symbol"]) as! NSString])
        try remSet(context, "setBadge:", badge)
    }
    func write(_ request: [String: Any], action: String) throws -> [String: Any] {
        let kind = text(request["kind"])
        let fields = request["fields"] as? [String: Any] ?? [:]
        let allowed: Set<String> = kind == "group" ? (action == "create" ? ["title", "account_id"] : ["title"]) :
            (action == "create" ? ["title", "account_id", "color", "emoji", "symbol", "group_id"] : ["title", "color", "emoji", "symbol", "group_id"])
        guard Set(fields.keys).isSubset(of: allowed), fields.values.allSatisfy({ $0 is String }),
              action == "delete" || !fields.isEmpty else { throw Failure(code: "invalid_fields", message: "清单或群组包含不支持的字段。") }
        if fields["title"] != nil || action == "create" {
            guard !text(fields["title"]).trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                throw Failure(code: "invalid_title", message: "名称不能为空。")
            }
        }
        let color = fields["color"] == nil ? nil : try parseColor(text(fields["color"]))
        if fields["emoji"] != nil && fields["symbol"] != nil { throw Failure(code: "invalid_icon", message: "emoji 和 symbol 只能指定一个。") }
        if fields["emoji"] != nil {
            let value = text(fields["emoji"])
            guard value.count == 1, value.unicodeScalars.contains(where: { $0.properties.isEmojiPresentation || $0.value == 0xFE0F || ($0.properties.isEmoji && $0.value > 0x7F) }) else {
                throw Failure(code: "invalid_icon", message: "图标须为一个 emoji。")
            }
        }
        if fields["symbol"] != nil && !reminderSymbols.contains(text(fields["symbol"])) {
            throw Failure(code: "invalid_icon", message: "原生图标编号不受支持，请查询图标目录。")
        }
        let scoped = !ids(request["list_ids"]).isEmpty
        if scoped && (kind == "group" || action == "create" || action == "delete" || fields["group_id"] != nil) {
            throw Failure(code: "restricted_scope", message: "限制清单范围时仅支持清单改名和外观修改；结构管理需要完整范围。")
        }
        var current: [String: Any] = [:]
        if action != "create" {
            current = try detail(request)
            guard let expected = request["expected"] as? [String: Any], NSDictionary(dictionary: current).isEqual(to: expected) else {
                throw Failure(code: "conflict", message: "清单或群组已变化，请重新查询并确认。")
            }
            guard current["writable"] as? Bool == true else { throw Failure(code: "read_only", message: "目标只读。") }
        }
        let accountID: String
        if action == "create" {
            if !text(fields["account_id"]).isEmpty { accountID = text(fields["account_id"]) }
            else if let defaultList = store.defaultCalendarForNewReminders() { accountID = remID(remObject(try list(defaultList.calendarIdentifier), "account")) }
            else { throw Failure(code: "invalid_fields", message: "没有默认提醒事项账户，请明确指定账户。") }
        } else { accountID = text(current["account_id"]) }
        guard let account = try accounts().first(where: { remID($0) == accountID }) else {
            throw Failure(code: "not_found", message: "目标账户不存在。")
        }
        let all = try snapshot(request)["containers"] as! [[String: Any]]
        if action == "create" && !all.contains(where: { text($0["kind"]) == "list" && text($0["account_id"]) == accountID && $0["writable"] as? Bool == true }) {
            throw Failure(code: "read_only", message: "无法核实可写的提醒事项账户；请先在 Apple 中建立一个普通清单。")
        }
        if let title = fields["title"] as? String, all.contains(where: { text($0["account_id"]) == accountID && text($0["title"]) == title && text($0["id"]) != text(request["id"]) }) {
            throw Failure(code: "duplicate_title", message: "同一账户已有同名清单或群组，请先澄清。")
        }
        var destination: NSObject? = nil
        if let groupID = fields["group_id"] as? String, !groupID.isEmpty {
            guard let expected = request["expected_destination"] as? [String: Any] else { throw Failure(code: "invalid_fields", message: "缺少目标群组审批基线。") }
            let actual = try detail(["kind": "group", "id": groupID, "list_ids": request["list_ids"] ?? []])
            guard NSDictionary(dictionary: actual).isEqual(to: expected) else { throw Failure(code: "conflict", message: "目标群组已变化，请重新确认。") }
            guard text(actual["account_id"]) == accountID else { throw Failure(code: "account_mismatch", message: "清单和群组必须属于同一账户。") }
            destination = try list(groupID)
        }
        let save = try saveRequest()
        if kind == "group" {
            let change: NSObject
            var id = text(request["id"])
            if action == "create" {
                let accountChange = try remCall(save, "updateAccount:", [account])
                change = try remCall(save, "addListWithName:toAccountChangeItem:", [text(fields["title"]) as NSString, accountChange])
                let selector = NSSelectorFromString("setIsGroup:")
                guard change.responds(to: selector) else { throw Failure(code: "unsupported_private_feature", message: "缺少群组创建接口。") }
                // ReminderKit 动态生成 setter；KVC 正确解包 BOOL，不能用 perform 传对象指针冒充布尔值。
                change.setValue(NSNumber(value: true), forKey: "isGroup")
                id = remID(change)
            } else {
                change = try remCall(save, "updateList:", [try list(id)])
                if action == "delete" {
                    for childID in current["child_list_ids"] as? [String] ?? [] {
                        let child = try list(childID)
                        let childChange = try remCall(save, "updateList:", [child])
                        try setGroup(childChange, nil, account)
                    }
                    let accountChange = try remCall(save, "updateAccount:", [account])
                    try remSet(change, "removeFromParentWithAccountChangeItem:", accountChange)
                } else { try remSet(change, "setName:", text(fields["title"]) as NSString) }
            }
            try self.save(save)
            store.reset(); membershipCache.removeAll()
            do {
                let fresh = try PersonalReminderContainers()
                if action == "delete" {
                    let rows = try fresh.snapshot(request)["containers"] as! [[String: Any]]
                    guard !rows.contains(where: { text($0["id"]) == id }),
                          (current["child_list_ids"] as? [String] ?? []).allSatisfy({ childID in rows.contains { text($0["id"]) == childID && text($0["group_id"]).isEmpty } }) else {
                        throw Failure(code: "native_error", message: "无法核实群组删除与清单保留结果。")
                    }
                    return ["deleted": true, "id": id, "preserved_list_ids": current["child_list_ids"] ?? []]
                }
                let result = try fresh.detail(["kind": kind, "id": id])
                guard text(result["title"]) == text(fields["title"]) else { throw Failure(code: "native_error", message: "无法核实群组名称。") }
                return ["item": result]
            } catch { throw Failure(code: "native_error", message: "群组保存后核对失败，请先查询核实：\(error)") }
        }
        let calendar: EKCalendar
        if action == "create" {
            let sources = store.calendars(for: .reminder).filter { calendar in
                (try? remID(remObject(list(calendar.calendarIdentifier), "account"))) == accountID && calendar.allowsContentModifications
            }.map { $0.source! }
            guard let source = sources.first else { throw Failure(code: "read_only", message: "无法确定可写的提醒事项账户。") }
            calendar = EKCalendar(for: .reminder, eventStore: store)
            calendar.source = source
            calendar.title = text(fields["title"])
        } else {
            guard let found = try selectedCalendars(request, entity: .reminder).first(where: { $0.calendarIdentifier == text(request["id"]) }) else {
                throw Failure(code: "not_found", message: "清单不存在。")
            }
            calendar = found
        }
        if action == "delete" {
            guard current["item_count"] as? Int == 0 else { throw Failure(code: "nonempty_list", message: "清单含任务（包括已完成任务），不能删除。") }
            try store.removeCalendar(calendar, commit: true)
            return ["deleted": true, "id": calendar.calendarIdentifier]
        }
        // 提前核实私有写接口，避免保存公开字段后才发现接口缺失。
        if fields["emoji"] != nil || fields["symbol"] != nil || fields["group_id"] != nil {
            let templateID = action == "create" ? text(all.first { text($0["kind"]) == "list" && text($0["account_id"]) == accountID }?["id"]) : calendar.calendarIdentifier
            let template = try remCall(save, "updateList:", [try list(templateID)])
            if fields["group_id"] != nil && (!template.responds(to: NSSelectorFromString("setParentOwnerID:")) || !template.responds(to: NSSelectorFromString("setParentSubContainerID:"))) {
                throw Failure(code: "unsupported_private_feature", message: "系统缺少群组写接口。")
            }
            try setIcon(template, fields)
        }
        if let title = fields["title"] as? String { calendar.title = title }
        if let color { calendar.color = color }
        var wrote = false
        if action == "create" || fields["title"] != nil || color != nil {
            try store.saveCalendar(calendar, commit: true)
            wrote = true
        }
        let persistedID = calendar.calendarIdentifier
        do {
            if fields["emoji"] != nil || fields["symbol"] != nil || fields["group_id"] != nil {
                // EventKit 新建清单后重新建立 ReminderKit store，避免旧视图找不到新对象。
                let bridge = try PersonalReminderContainers()
                let nativeSave = try bridge.saveRequest()
                let object = try bridge.list(calendar.calendarIdentifier)
                let change = try remCall(nativeSave, "updateList:", [object])
                try bridge.setIcon(change, fields)
                if fields["group_id"] != nil { try bridge.setGroup(change, destination, account) }
                try bridge.save(nativeSave)
                wrote = true
            }
            store.reset(); membershipCache.removeAll()
            let result = try PersonalReminderContainers().detail(["kind": "list", "id": persistedID, "list_ids": request["list_ids"] ?? []])
            for field in ["title", "group_id", "emoji", "symbol"] where fields[field] != nil {
                guard text(result[field]) == text(fields[field]) else { throw Failure(code: "native_error", message: "无法核实清单字段：\(field)") }
            }
            if let color, text(result["color"]) != colorHex(color) { throw Failure(code: "native_error", message: "无法核实清单颜色。") }
            return ["item": result]
        } catch {
            // 两种原生接口不能组成同一事务。公开字段已保存后出错，必须保留未知写入保护。
            if wrote { throw Failure(code: "native_error", message: "清单部分字段可能已保存，请先核实，不能自动重试：\(error)") }
            throw error
        }
    }
}


// Extended Apple adapters reuse the legacy ReminderKit selectors, without its SQLite reader.
func nativeObject(_ name: String) throws -> NSObject {
    guard let cls = NSClassFromString(name) as? NSObject.Type,
          let value = cls.perform(NSSelectorFromString("alloc"))?.takeUnretainedValue() as? NSObject else {
        throw Failure(code:"unsupported_private_feature",message:"系统缺少接口：\(name)")
    }
    return value
}
func nativeSection(_ native: PersonalReminderContainers, _ id: String) throws -> NSObject {
    guard UUID(uuidString:id) != nil,
          let cls = NSClassFromString("REMObjectID") as? NSObject.Type,
          let object = cls.perform(NSSelectorFromString("objectIDWithURL:"),with:NSURL(string:"x-apple-reminderkit://REMCDListSection/\(id)"))?.takeUnretainedValue() as? NSObject,
          let section = try remFetch(native.remStore,"fetchListSectionWithObjectID:error:",object) as? NSObject else {
        throw Failure(code:"not_found",message:"分区不存在。")
    }
    return section
}
// One request/process only; invalidated after every save. No persistent store.
var membershipCache:[String:[String:String]] = [:]
func sectionInvocationParameters(_ listID: String) -> [String: Any] {
    let configuration:[String:Any] = ["sortingStyle":["base":"manual"],"showCompleted":["base":"on"],
        "countCompleted":true,"remindersPrefetch":["base":"none"],"fetchSubtasks":"on"]
    // macOS 27 requires this Bool beside list and configuration when decoding.
    return ["list":["objectIDUUID":listID],"fetchPredefinedGroceryListSections":false,"configuration":configuration]
}

func sectionMemberships(_ list: NSObject) throws -> [String:String] {
    let id=remID(list)
    if let cached=membershipCache[id] { return cached }
    guard let bundle=Bundle(path:"/System/Library/PrivateFrameworks/ReminderKitInternal.framework"), bundle.load(),
          let cls=NSClassFromString("REMRemindersListDataView_ListSectionsInvocation") as? NSObject.Type,
          let allocated=cls.perform(NSSelectorFromString("alloc"))?.takeUnretainedValue() as? NSObject,
          allocated.responds(to:NSSelectorFromString("initWithFetchResultTokenToDiffAgainst:")),
          let invocation=allocated.perform(NSSelectorFromString("initWithFetchResultTokenToDiffAgainst:"),with:nil)?.takeUnretainedValue(),
          let nativeStore=remObject(list,"store"), let account=remObject(list,"account"),
          let listID=remObject(list,"remObjectID"), let listStorage=remObject(list,"storage"),
          let accountID=remObject(account,"remObjectID"), let accountStorage=remObject(account,"storage") else {
        throw Failure(code:"unsupported_private_feature",message:"当前系统未提供分区模型读取接口。")
    }
    // Native ListSectionsInvocation uses Codable property lists and an object-ID/storage dictionary.
    let parameters=sectionInvocationParameters(id)
    let data=try PropertyListSerialization.data(fromPropertyList:parameters,format:.binary,options:0)
    let selector=NSSelectorFromString("resultFromPerformingSwiftInvocation:parametersData:storages:error:")
    guard let method=class_getInstanceMethod(type(of:nativeStore),selector) else { throw Failure(code:"unsupported_private_feature",message:"系统缺少分区查询接口。") }
    let fn=unsafeBitCast(method_getImplementation(method),to:(@convention(c)(AnyObject,Selector,AnyObject,AnyObject,AnyObject,UnsafeMutablePointer<NSError?>)->AnyObject?).self)
    var storages:[NSObject:NSObject] = [listID:listStorage,accountID:accountStorage]
    var parent=remObject(list,"parentList")
    while let object=parent {
        guard let objectID=remObject(object,"remObjectID"), let storage=remObject(object,"storage"), storages[objectID]==nil else { break }
        storages[objectID]=storage;parent=remObject(object,"parentList")
    }
    var error:NSError?
    guard let response=fn(nativeStore,selector,invocation,data as NSData,storages as NSDictionary,&error) as? NSObject,
          let resultData=remValue(response,"resultData") as? Data,
          let root=try PropertyListSerialization.propertyList(from:resultData,format:nil) as? [String:Any],
          let model=root["model"] as? [String:Any], let sectionsModel=model["sectionsModel"] as? [String:Any],
          let sections=sectionsModel["sections"] as? [[String:Any]] else {
        throw Failure(code:"read_failed",message:"无法完整读取原生分区模型：\(String(describing:error))")
    }
    var result:[String:String] = [:]
    for section in sections {
        guard let type=section["type"] as? [String:Any] else { throw Failure(code:"read_failed",message:"分区模型缺少类型。") }
        if type["sectionless"] != nil { continue }
        guard let sectioned=type["sectioned"] as? [String:Any], let objectID=sectioned["sectionID"] as? [String:Any],
              let uuid=objectID["uuid"] as? String, UUID(uuidString:uuid) != nil,
              let reminders=section["reminders"] as? [[String:Any]] else { throw Failure(code:"read_failed",message:"分区模型格式变化，不能确定事项归属。") }
        for reminder in reminders {
            guard let objectID=reminder["objectID"] as? [String:Any], let memberID=objectID["uuid"] as? String else { throw Failure(code:"read_failed",message:"分区成员缺少编号。") }
            result[memberID.uppercased()]=uuid.uppercased()
        }
    }
    membershipCache[id]=result
    return result
}
func sectionRows(_ request:[String:Any]) throws -> [[String:Any]] {
    let native = try PersonalReminderContainers()
    let view = try remCall(nativeObject("REMListSectionsDataView"),"initWithStore:",[native.remStore])
    var result:[[String:Any]] = []
    for calendar in try selectedCalendars(request,entity:.reminder) {
        let list = try native.list(calendar.calendarIdentifier)
        let memberships = try? sectionMemberships(list)
        guard let rows = try remFetch(view,"fetchListSectionsInList:error:",list) as? [NSObject] else {
            throw Failure(code:"read_failed",message:"无法读取清单分区。")
        }
        for section in rows {
            let id = remID(section)
            result.append(["id":id,"kind":"section","title":text(remValue(section,"displayName")),
                "calendar_id":calendar.calendarIdentifier,"writable":calendar.allowsContentModifications,
                "member_ids":memberships.map{$0.filter { $0.value==id.uppercased() }.keys.sorted()} as Any? ?? NSNull(), "membership_readable":memberships != nil])
        }
    }
    return result.sorted { text($0["id"])<text($1["id"]) }
}
func eventCalendarRows(_ request:[String:Any]) throws -> [[String:Any]] {
    try selectedCalendars(request,entity:.event).map { calendar in
        ["id":calendar.calendarIdentifier,"kind":"event_calendar","title":calendar.title,
         "color":colorHex(calendar.color),"account_id":calendar.source.sourceIdentifier,
         "account_title":calendar.source.title,"writable":calendar.allowsContentModifications]
    }
}
func extraContainerRows(_ request:[String:Any]) throws -> [[String:Any]] {
    if text(request["kind"])=="event_calendar" { return try eventCalendarRows(request) }
    return try sectionRows(request)
}
func extraContainerDetail(_ request:[String:Any]) throws -> [String:Any] {
    guard let row = try extraContainerRows(request).first(where:{text($0["id"])==text(request["id"]) && text($0["kind"])==text(request["kind"])}) else {
        throw Failure(code:"not_found",message:"分区或日历不存在，或不在同步范围。")
    }
    return row
}
func writeExtraContainer(_ request:[String:Any], action:String) throws -> [String:Any] {
    let kind=text(request["kind"]), fields=request["fields"] as? [String:Any] ?? [:]
    let allowed:Set<String> = kind=="section" ? (action=="create" ? ["title","calendar_id"] : ["title"]) : (action=="create" ? ["title","color","account_id"] : ["title","color"])
    guard Set(fields.keys).isSubset(of:allowed), fields.values.allSatisfy({$0 is String}) else { throw Failure(code:"invalid_fields",message:"容器字段无效。") }
    if action=="create" || fields["title"] != nil {
        guard !text(fields["title"]).trimmingCharacters(in:.whitespacesAndNewlines).isEmpty else { throw Failure(code:"invalid_title",message:"名称不能为空。") }
    }
    var current:[String:Any] = [:]
    if action != "create" {
        current = try extraContainerDetail(request)
        guard let expected=request["expected"] as? [String:Any], NSDictionary(dictionary:current).isEqual(to:expected) else { throw Failure(code:"conflict",message:"容器已变化，请重新确认。") }
        guard current["writable"] as? Bool == true else { throw Failure(code:"read_only",message:"容器不可写。") }
    }
    var id=text(request["id"])
    if kind=="section" {
        let calendarID=action=="create" ? text(fields["calendar_id"]) : text(current["calendar_id"])
        guard let calendar=try selectedCalendars(request,entity:.reminder).first(where:{$0.calendarIdentifier==calendarID}), calendar.allowsContentModifications else { throw Failure(code:"read_only",message:"分区所在清单不可写或不在范围。") }
        if action=="delete", (current["membership_readable"] as? Bool != true || !(current["member_ids"] as? [String] ?? []).isEmpty) { throw Failure(code:"invalid_fields",message:"分区内仍有事项，请先移出或删除。") }
        let native=try PersonalReminderContainers(), save=try native.saveRequest()
        if action=="create" {
            let listChange=try remCall(save,"updateList:",[try native.list(calendarID)])
            let context=try remCall(listChange,"sectionsContextChangeItem")
            let change=try remCall(save,"addListSectionWithDisplayName:toListSectionContextChangeItem:",[text(fields["title"]) as NSString,context])
            id=remID(change)
        } else {
            let change=try remCall(save,"updateListSection:",[try nativeSection(native,id)])
            if action=="delete" {
                guard change.responds(to:NSSelectorFromString("removeFromList")) else { throw Failure(code:"unsupported_private_feature",message:"系统不支持分区删除。") }
                _=change.perform(NSSelectorFromString("removeFromList"))
            } else { try remSet(change,"setDisplayName:",text(fields["title"]) as NSString) }
        }
        try native.save(save)
    } else {
        if !ids(request["calendar_ids"]).isEmpty && action != "update" { throw Failure(code:"restricted_scope",message:"日历创建或删除需要完整日历范围。") }
        let calendar:EKCalendar
        if action=="create" {
            let writable=store.calendars(for:.event).filter{$0.allowsContentModifications}
            let sourceID=text(fields["account_id"])
            let source=sourceID.isEmpty ? store.defaultCalendarForNewEvents?.source : writable.first{$0.source.sourceIdentifier==sourceID}?.source
            guard let source, writable.contains(where:{$0.source.sourceIdentifier==source.sourceIdentifier}) else { throw Failure(code:"read_only",message:"无法确定可写的日历账户。") }
            calendar=EKCalendar(for:.event,eventStore:store); calendar.source=source
        } else {
            guard let found=store.calendar(withIdentifier:id) else { throw Failure(code:"not_found",message:"日历不存在。") }
            calendar=found
        }
        if action=="delete" { try store.removeCalendar(calendar,commit:true) }
        else {
            if let title=fields["title"] as? String { calendar.title=title }
            if let color=fields["color"] as? String { calendar.color=try parseColor(color) }
            try store.saveCalendar(calendar,commit:true); id=calendar.calendarIdentifier
        }
    }
    do {
        store.reset(); membershipCache.removeAll()
        var lookup=request; lookup["id"]=id
        if action=="delete" {
            guard !(try extraContainerRows(request)).contains(where:{text($0["id"])==id}) else { throw Failure(code:"read_failed",message:"容器仍然存在。") }
            return ["deleted":true,"id":id]
        }
        let result=try extraContainerDetail(lookup)
        if let title=fields["title"] as? String, text(result["title"]) != title { throw Failure(code:"read_failed",message:"容器名称未保存。") }
        if let color=fields["color"] as? String, text(result["color"]) != colorHex(try parseColor(color)) { throw Failure(code:"read_failed",message:"容器颜色未保存。") }
        return ["item":result]
    } catch { throw Failure(code:"unknown_write",message:"容器已提交，结果需核实：\(error)") }
}

func attachmentRows(_ object:NSObject) throws -> [[String:Any]] {
    let context=try remCall(object,"attachmentContext")
    guard let rows=remValue(context,"attachments") as? [NSObject] else { throw Failure(code:"read_failed",message:"无法读取附件。") }
    return rows.map { a in
        let file=remValue(a,"fileURL") as? URL, url=remValue(a,"url") as? URL
        return ["id":remID(a),"type":url == nil ? "image" : "url","file_name":file?.lastPathComponent ?? "", "url":url?.absoluteString ?? ""]
    }.sorted {text($0["id"])<text($1["id"])}
}
func extendedReminderState(_ object:NSObject) throws -> [String:Any] {
    let context=remObject(object,"subtaskContext")
    let children = try context.map { try remFetch($0,"fetchRemindersWithError:") as? [NSObject] ?? [] } ?? []
    let completed = try context.map { try remFetch($0,"fetchObjectIDsOfCompletedSubtasksWithError:") as? [NSObject] ?? [] } ?? []
    let urgent=remValue(remObject(object,"urgentAlarmContext"),"isUrgentStateEnabledForCurrentUser") as? NSNumber
    guard let list=remObject(object,"list") else {
        throw Failure(code:"read_failed",message:"无法读取父子关系或紧急状态。")
    }
    var sectionID:Any=NSNull(), sectionError=""
    do { let memberships=try sectionMemberships(list); sectionID=memberships[remID(object).uppercased()] ?? memberships[remID(remObject(object,"parentReminder")).uppercased()] ?? "" }
    catch { sectionError=String(describing:error) }
    return ["parent_id":remID(remObject(object,"parentReminder")),
        "subtask_ids":Set(children.map{remID($0)}+completed.map{remID($0)}).sorted(),
        "section_id":sectionID, "section_read_error":sectionError,
        "urgent":urgent?.boolValue as Any? ?? NSNull(), "urgent_supported":urgent != nil,"attachments":try attachmentRows(object)]
}
func setReminderAttachments(_ change:NSObject, specs:[[String:Any]]) throws {
    let context=try remCall(change,"attachmentContext")
    guard let existing=remValue(context,"attachments") as? [NSObject] else { throw Failure(code:"read_failed",message:"无法读取待修改附件。") }
    let keep=Set(specs.compactMap{$0["id"] as? String})
    guard keep.isSubset(of:Set(existing.map{remID($0)})) else { throw Failure(code:"invalid_field",message:"保留附件不属于该事项。") }
    for a in existing where !keep.contains(remID(a)) { try remSet(context,"removeAttachment:",a) }
    for spec in specs {
        guard spec.count==1 else { throw Failure(code:"invalid_field",message:"每个附件只接受一个来源。") }
        if spec["id"] != nil { continue }
        if let raw=spec["url"] as? String {
            guard let url=URL(string:raw), ["http","https"].contains(url.scheme ?? ""), url.host != nil else { throw Failure(code:"invalid_url",message:"网页附件URL无效。") }
            _=try remCall(context,"addURLAttachmentWithURL:",[url as NSURL]);continue
        }
        let path=text(spec["file_path"])
        guard path.hasPrefix("/"), let image=NSImage(contentsOfFile:path), let representation=image.representations.first else { throw Failure(code:"invalid_field",message:"提醒事项附件仅接受已有的图片文件或网页链接。") }
        let selector=NSSelectorFromString("addImageAttachmentWithURL:width:height:error:")
        guard let method=class_getInstanceMethod(type(of:context),selector) else { throw Failure(code:"unsupported_private_feature",message:"系统不支持图片附件。") }
        let fn=unsafeBitCast(method_getImplementation(method),to:(@convention(c)(AnyObject,Selector,AnyObject,UInt64,UInt64,UnsafeMutablePointer<NSError?>)->AnyObject?).self)
        var error:NSError?
        guard fn(context,selector,URL(fileURLWithPath:path) as NSURL,UInt64(max(1,representation.pixelsWide)),UInt64(max(1,representation.pixelsHigh)),&error) != nil else { throw Failure(code:"invalid_field",message:error?.localizedDescription ?? "无法添加图片附件。") }
    }
}
func setExtendedReminder(_ native:PersonalReminderContainers, _ save:NSObject, _ change:NSObject, fields:[String:Any], old:NSObject?) throws {
    if let urgent=fields["urgent"] {
        guard let number=urgent as? NSNumber, CFGetTypeID(number)==CFBooleanGetTypeID() else { throw Failure(code:"invalid_field",message:"紧急状态须为布尔值。") }
        let context=try remCall(change,"urgentAlarmContext")
        guard context.responds(to:NSSelectorFromString("setIsUrgentStateEnabledForCurrentUser:")) else { throw Failure(code:"unsupported_private_feature",message:"系统不支持紧急提醒。") }
        context.setValue(number,forKey:"isUrgentStateEnabledForCurrentUser")
    }
    if let specs=fields["attachments"] as? [[String:Any]] { try setReminderAttachments(change,specs:specs) }
    if let parent=fields["parent_id"] as? String {
        if parent.isEmpty && old != nil {
            guard change.responds(to:NSSelectorFromString("removeFromParentReminder")) else { throw Failure(code:"unsupported_private_feature",message:"系统不支持移出父任务。") }
            _=change.perform(NSSelectorFromString("removeFromParentReminder"))
            guard let list=remObject(old,"list") else { throw Failure(code:"invalid_field",message:"缺少原清单。") }
            try remSet(change,"setListID:",remObject(list,"remObjectID"))
        } else if !parent.isEmpty {
            let parentChange=try remCall(save,"updateReminder:",[try PersonalReminderAlerts().reminder(parent)])
            try remSet(try remCall(parentChange,"subtaskContext"),"addReminderChangeItem:",change)
        }
    }
    if let sectionID=fields["section_id"] as? String {
        let listID = text(fields["calendar_id"])
        let list = !listID.isEmpty ? try native.list(listID) : remObject(old,"list")
        guard let list else { throw Failure(code:"invalid_field",message:"缺少分区所属清单。") }
        let prior = old == nil ? "" : try sectionMemberships(list)[remID(old).uppercased()] ?? ""
        let targetID=sectionID.isEmpty ? prior : sectionID
        if !targetID.isEmpty {
            let section=try nativeSection(native,targetID)
            let listChange=try remCall(save,"updateList:",[list])
            let context=try remCall(listChange,"sectionsContextChangeItem")
            guard let membership=makeMembership(memberObjectID:remObject(change,"remObjectID")!,groupObjectID:sectionID.isEmpty ? nil : remObject(section,"remObjectID"),isObsolete:false), let memberships=makeMemberships([membership]) else { throw Failure(code:"unsupported_private_feature",message:"系统不支持分区归属写入。") }
            try remSet(context,"setUnsavedMembershipsOfRemindersInSections:",memberships)
        }
    }
}
func validateReminderRelations(_ request:[String:Any], _ item:EKReminder, fields:[String:Any]) throws {
    let native=try PersonalReminderAlerts()
    let existing = text(request["action"]) == "create" ? nil : try native.reminder(item.calendarItemIdentifier)
    let current = try existing.map{try extendedReminderState($0)} ?? [:]
    if text(request["action"])=="delete" && !(current["subtask_ids"] as? [String] ?? []).isEmpty { throw Failure(code:"invalid_fields",message:"父任务仍包含子任务，请先处理子任务。") }
    if (fields["urgent"] as? Bool ?? current["urgent"] as? Bool ?? false), item.dueDateComponents?.hour == nil { throw Failure(code:"invalid_date",message:"紧急提醒需要明确到期时刻。") }
    for (field,kind) in [("parent_id","reminder"),("section_id","section")] {
        guard let id=fields[field] as? String, !id.isEmpty else { continue }
        let actual = kind=="reminder" ? try payload(target(request,kind:kind,id:id)) : try extraContainerDetail(request.merging(["kind":kind,"id":id]){_,n in n})
        let expected = (request["expected_references"] as? [String:[String:Any]])?[field]
        guard let expected, NSDictionary(dictionary:comparableAppleState(actual)).isEqual(to:comparableAppleState(expected)) else { throw Failure(code:"conflict",message:"目标父任务或分区变化，请重新确认。") }
        guard text(actual["calendar_id"])==item.calendar.calendarIdentifier else { throw Failure(code:"invalid_fields",message:"父任务、分区和事项必须属于同一清单。") }
        if field=="section_id" && !text(current["parent_id"]).isEmpty { throw Failure(code:"invalid_fields",message:"子任务随父任务归属分区，请修改父任务的分区。") }
        if field=="parent_id" {
            guard text(actual["parent_id"]).isEmpty, (current["subtask_ids"] as? [String] ?? []).isEmpty else { throw Failure(code:"invalid_fields",message:"Apple 提醒事项不支持多层子任务；请先解除嵌套关系。") }
            var cursor=id;var visited=Set<String>()
            while !cursor.isEmpty {
                guard cursor != item.calendarItemIdentifier, !visited.contains(cursor) else { throw Failure(code:"invalid_fields",message:"不能形成循环父子关系。") }
                visited.insert(cursor);cursor=remID(remObject(try native.reminder(cursor),"parentReminder"))
            }
        }
    }
    if fields["calendar_id"] != nil, let existing, remID(remObject(existing,"list")) != item.calendar.calendarIdentifier, current["section_id"] is NSNull {
        throw Failure(code:"unsupported_private_feature",message:"无法核实分区归属，不能跨清单移动。")
    }
    if fields["calendar_id"] != nil, let existing, remID(remObject(existing,"list")) != item.calendar.calendarIdentifier,
       !(text(current["parent_id"]).isEmpty && text(current["section_id"]).isEmpty && (current["subtask_ids"] as? [String] ?? []).isEmpty) {
        throw Failure(code:"invalid_fields",message:"跨清单移动前请先解除父子关系和分区归属。")
    }
}
func travelTime(_ event:EKEvent) -> Double? {
    let sel=NSSelectorFromString("travelTime")
    guard let method=class_getInstanceMethod(type(of:event),sel) else{return nil}
    return unsafeBitCast(method_getImplementation(method),to:(@convention(c)(AnyObject,Selector)->Double).self)(event,sel)/60
}
func setTravel(_ event:EKEvent,_ value:Any) throws {
    let minutes=try alarmMinutes(value)
    guard minutes<=10080, let method=class_getInstanceMethod(type(of:event),NSSelectorFromString("setTravelTime:")) else { throw Failure(code:"unsupported_private_feature",message:"系统不支持日程出行时间，或时间超出范围。") }
    unsafeBitCast(method_getImplementation(method),to:(@convention(c)(AnyObject,Selector,Double)->Void).self)(event,NSSelectorFromString("setTravelTime:"),Double(minutes)*60)
}

func makeMembership(memberObjectID: AnyObject, groupObjectID: AnyObject?, isObsolete: Bool = false) -> NSObject? {
    guard let cls = NSClassFromString("REMMembership") as? NSObject.Type,
          let object = cls.perform(NSSelectorFromString("alloc"))?.takeUnretainedValue() as? NSObject,
          let memberUUID = (memberObjectID as AnyObject).perform(NSSelectorFromString("uuid"))?.takeUnretainedValue() as? NSUUID else {
        return nil
    }
    let groupUUID = (groupObjectID as? NSObject).flatMap { remValue($0,"uuid") as? NSUUID }
    let selector = NSSelectorFromString("initWithMemberIdentifier:groupIdentifier:isObsolete:modifiedOn:")
    guard let method = class_getInstanceMethod(cls, selector) else {
        return nil
    }
    let fn = unsafeBitCast(
        method_getImplementation(method),
        to: (@convention(c) (AnyObject, Selector, AnyObject, AnyObject?, Bool, AnyObject) -> AnyObject).self
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


do {
    let input = FileHandle.standardInput.readDataToEndOfFile()
    let request = try JSONSerialization.jsonObject(with: input) as? [String: Any] ?? [:]
    let action = text(request["action"])
    if action == "status" {
        emit(["ok": true, "data": permissionStatus()])
    } else if action == "search_locations" {
        emit(["ok": true, "data": try searchLocations(request)])
    } else if action == "authorize" {
        try authorize()
        emit(["ok": true, "data": permissionStatus()])
    } else {
        try requireAccess()
        if action == "watch" {
            let observer = NotificationCenter.default.addObserver(forName: .EKEventStoreChanged, object: nil, queue: .main) { _ in
                emit(["ok": true, "data": ["changed": true]])
            }
            emit(["ok": true, "data": ["ready": true]])
            RunLoop.main.run()
            NotificationCenter.default.removeObserver(observer)
        } else if action == "snapshot" {
            emit(["ok": true, "data": try snapshot(request)])
        } else if action == "detail" {
            let item = ["section","event_calendar"].contains(text(request["kind"])) ? try extraContainerDetail(request) : ["list", "group"].contains(text(request["kind"]))
                ? try PersonalReminderContainers().detail(request)
                : try payload(target(request, kind: text(request["kind"]), id: text(request["id"])))
            emit(["ok": true, "data": ["item": item]])
        } else if ["create", "update", "delete"].contains(action) {
            let result = ["section","event_calendar"].contains(text(request["kind"])) ? try writeExtraContainer(request,action:action) : ["list", "group"].contains(text(request["kind"]))
                ? try PersonalReminderContainers().write(request, action: action)
                : try write(request, action: action)
            emit(["ok": true, "data": result])
        } else { throw Failure(code: "unsupported_action", message: "不支持的操作。") }
    }
} catch let error as Failure {
    emit(["ok": false, "error": ["code": error.code, "message": error.message]])
} catch {
    emit(["ok": false, "error": ["code": "native_error", "message": String(describing: error)]])
}
