// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
//
// Engine.swift: the app's one engine. Wraps the bridge's fms_host_t, plays it
// through an AVAudioSourceNode and publishes what the views draw. Players load
// on a background queue; every fms_host_* call except the render runs here on
// the main thread.

import Foundation
import AVFoundation
import CoreAudio

// Host-only shared-device control. No setter is called except request/restore.
struct DeviceRateSnapshot: Equatable {
    var id: AudioObjectID = 0
    var uid = ""
    var alive = false
    var nominal = 0.0
    var output = 0.0
    var ranges: [ClosedRange<Double>] = []
    var settable = false
    var error: String?
    func offers(_ rate: Double) -> Bool {
        id != 0 && !uid.isEmpty && alive && nominal.isFinite && nominal > 0 && settable &&
        rate.isFinite && rate > 0 && ranges.contains { $0.contains(rate) }
    }
    func sameDevice(_ other: Self) -> Bool { id == other.id && uid == other.uid && id != 0 && !uid.isEmpty }
}

@MainActor protocol DeviceRateBackend: AnyObject {
    func read() -> DeviceRateSnapshot
    func observe(generation: UInt64, _ changed: @escaping (UInt64, Bool) -> Void) -> String?
    func write(_ rate: Double, expected: DeviceRateSnapshot) -> String?
}

@MainActor final class DeviceRateControl: ObservableObject {
    enum Phase: String { case idle, requesting, confirmed, error }
    private struct Receipt {
        var generation: UInt64
        var device: DeviceRateSnapshot
        var baseline: Double
        var target: Double
        var confirmed = false
    }
    @Published private(set) var snapshot = DeviceRateSnapshot()
    @Published private(set) var phase = Phase.idle
    @Published private(set) var message = ""
    private(set) var generation: UInt64 = 0
    private var receipt: Receipt?
    private var observing = false
    let backend: DeviceRateBackend
    var didRead: ((DeviceRateSnapshot) -> Void)?

    init(_ backend: DeviceRateBackend) { self.backend = backend; refresh(); if !observing { bind() } }
    private func bind() {
        let error = backend.observe(generation: generation) { [weak self] token, nominal in
            guard let self, token == self.generation else { return }
            self.refresh(nominalNotification: nominal)
        }
        observing = error == nil
        if let error { phase = .error; message = error }
    }
    var pending: Bool { phase == .requesting }
    var restoreRate: Double? {
        guard let r = receipt, r.confirmed, snapshot.sameDevice(r.device), snapshot.nominal == r.target,
              r.target != r.baseline, snapshot.offers(r.baseline) else { return nil }
        return r.baseline
    }
    func canRequest(_ rate: Double) -> Bool { observing && !pending && snapshot.offers(rate) && snapshot.nominal != rate }

    func refresh(nominalNotification: Bool = false) {
        let next = backend.read(), routeChanged = snapshot.id != next.id || snapshot.uid != next.uid
        snapshot = next
        if let r = receipt {
            if !next.sameDevice(r.device) || !next.alive || !next.nominal.isFinite || next.nominal <= 0 {
                receipt = nil; phase = .error; message = "The output device changed; the prior rate request and restore receipt are no longer valid."
            } else if next.nominal != r.target && (r.confirmed || next.nominal != r.device.nominal) {
                receipt = nil; phase = .error; message = "The device rate changed externally; the prior restore receipt was cleared."
            } else if nominalNotification && next.nominal == r.target {
                if r.target == r.baseline { receipt = nil; message = "Device rate restored to \(Self.rate(r.target)) Hz." }
                else { receipt?.confirmed = true; message = "Device rate confirmed at \(Self.rate(r.target)) Hz." }
                phase = .confirmed
            }
        }
        if routeChanged {
            generation &+= 1
            bind() // queued callbacks from the former route cannot complete a new request
        }
        didRead?(next)
    }

    private func show(_ error: String?) -> String? {
        if let error { if !pending { phase = .error }; message = error }
        return error
    }
    @discardableResult func request(_ rate: Double) -> String? { show(request(rate, restoring: nil)) }
    private func request(_ rate: Double, restoring expected: DeviceRateSnapshot?) -> String? {
        guard !pending else { return "A device-rate request is awaiting confirmation." }
        refresh()
        if let expected, !snapshot.sameDevice(expected) || restoreRate != rate {
            return "The restore receipt changed before the request; no rate was written."
        }
        guard canRequest(rate) else { return "This device does not offer a different, settable \(Self.rate(rate)) Hz rate." }
        let old = receipt, current = snapshot
        // : an expired same-device target preserves its original
        // baseline only when fresh readback still equals that target.
        let baseline = old.flatMap { current.sameDevice($0.device) && current.nominal == $0.target ? $0.baseline : nil } ?? current.nominal
        generation &+= 1
        bind()
        guard observing else { return message }
        receipt = Receipt(generation: generation, device: current, baseline: baseline, target: rate)
        phase = .requesting; message = "Requesting \(Self.rate(rate)) Hz; waiting for device notification and readback."
        if let error = backend.write(rate, expected: current) {
            let actual = backend.read(); snapshot = actual
            if var old, actual.sameDevice(old.device), actual.nominal == old.target, old.confirmed {
                old.generation = generation; receipt = old
            } else { receipt = nil }
            phase = .error; message = error; didRead?(actual); return error
        }
        // A read is useful for the label but never confirms a setter alone.
        refresh()
        return nil
    }
    @discardableResult func restore() -> String? {
        guard let rate = restoreRate else { return show("There is no valid rate to restore for this device.") }
        return show(request(rate, restoring: snapshot))
    }
    func timeout(_ token: UInt64) {
        guard token == generation, pending else { return }
        refresh()
        guard receipt?.generation == token, pending else { return }
        phase = .error
        message = "Device-rate confirmation timed out after 5 seconds. Actual nominal rate: \(Self.rate(snapshot.nominal)) Hz. No retry or rollback was made."
    }
    static func rate(_ rate: Double) -> String {
        guard rate.isFinite, rate > 0 else { return "unavailable" }
        return rate.rounded() == rate ? String(format: "%.0f", rate) : String(rate)
    }
}

@MainActor final class HardwareDeviceRate: DeviceRateBackend {
    private struct Listener { var object: AudioObjectID; var address: AudioObjectPropertyAddress; var block: AudioObjectPropertyListenerBlock }
    private var listeners: [Listener] = []
    private let output: () -> Double
    init(output: @escaping () -> Double) { self.output = output }
    private func address(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    }
    private func readValue<T>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector, _ initial: T) -> T? {
        var a = address(selector), value = initial, size = UInt32(MemoryLayout<T>.size)
        let result = withUnsafeMutableBytes(of: &value) { AudioObjectGetPropertyData(object, &a, 0, nil, &size, $0.baseAddress!) }
        return result == noErr && size == MemoryLayout<T>.size ? value : nil
    }
    func read() -> DeviceRateSnapshot {
        var s = DeviceRateSnapshot(); s.output = output()
        guard let id = readValue(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultOutputDevice, AudioObjectID(0)), id != 0 else {
            s.error = "No default output device is available."; return s
        }
        s.id = id
        // AudioHardwareBase.h gives the caller ownership of this CFObject.
        if let wrapped = readValue(id, kAudioDevicePropertyDeviceUID, Optional<Unmanaged<CFString>>.none), let uid = wrapped {
            s.uid = uid.takeRetainedValue() as String
        }
        s.alive = readValue(id, kAudioDevicePropertyDeviceIsAlive, UInt32(0)) == 1
        s.nominal = readValue(id, kAudioDevicePropertyNominalSampleRate, Double(0)) ?? 0
        var a = address(kAudioDevicePropertyNominalSampleRate), writable: DarwinBoolean = false
        s.settable = AudioObjectIsPropertySettable(id, &a, &writable) == noErr && writable.boolValue
        a = address(kAudioDevicePropertyAvailableNominalSampleRates)
        var size: UInt32 = 0
        if AudioObjectGetPropertyDataSize(id, &a, 0, nil, &size) == noErr && size > 0 && size % UInt32(MemoryLayout<AudioValueRange>.stride) == 0 {
            var ranges = [AudioValueRange](repeating: AudioValueRange(mMinimum: 0, mMaximum: 0), count: Int(size) / MemoryLayout<AudioValueRange>.stride)
            let rc = ranges.withUnsafeMutableBytes { AudioObjectGetPropertyData(id, &a, 0, nil, &size, $0.baseAddress!) }
            if rc == noErr {
                s.ranges = ranges.compactMap { $0.mMinimum.isFinite && $0.mMaximum.isFinite && $0.mMinimum > 0 && $0.mMaximum >= $0.mMinimum ? $0.mMinimum...$0.mMaximum : nil }
            }
        }
        if !s.alive || s.uid.isEmpty || !s.nominal.isFinite || s.nominal <= 0 { s.error = "Output device identity or nominal rate is unavailable." }
        return s
    }
    /// Owner-invoked read-only probe: no listener, setter or engine start.
    static func probe() -> Int32 {
        let audio = AVAudioEngine()
        let snapshot = HardwareDeviceRate(output: { audio.outputNode.outputFormat(forBus: 0).sampleRate }).read()
        let identity = snapshot.uid.utf8.reduce(UInt64(14695981039346656037)) { ($0 ^ UInt64($1)) &* 1099511628211 }
        print("DEVICE_PROBE id=\(snapshot.id) uid_digest=\(String(identity, radix: 16)) alive=\(snapshot.alive) nominal=\(DeviceRateControl.rate(snapshot.nominal)) output=\(DeviceRateControl.rate(snapshot.output)) settable=\(snapshot.settable)")
        for r in snapshot.ranges { print("DEVICE_RANGE min=\(DeviceRateControl.rate(r.lowerBound)) max=\(DeviceRateControl.rate(r.upperBound))") }
        if let error = snapshot.error { print("DEVICE_PROBE error=\(error)") }
        print("DEVICE_PROBE writes=0")
        return snapshot.error == nil ? 0 : 1
    }
    private func removeListeners() {
        for var item in listeners { AudioObjectRemovePropertyListenerBlock(item.object, &item.address, .main, item.block) }
        listeners.removeAll()
    }
    func observe(generation: UInt64, _ changed: @escaping (UInt64, Bool) -> Void) -> String? {
        removeListeners()
        let id = read().id
        var properties: [(AudioObjectID, AudioObjectPropertySelector)] = [(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultOutputDevice)]
        if id != 0 { properties += [kAudioDevicePropertyNominalSampleRate, kAudioDevicePropertyDeviceIsAlive, kAudioDevicePropertyDeviceUID, kAudioDevicePropertyAvailableNominalSampleRates].map { (id, $0) } }
        for (object, selector) in properties {
            var a = address(selector)
            let block: AudioObjectPropertyListenerBlock = { _, _ in
                MainActor.assumeIsolated { changed(generation, selector == kAudioDevicePropertyNominalSampleRate) }
            }
            let rc = AudioObjectAddPropertyListenerBlock(object, &a, .main, block)
            guard rc == noErr else { removeListeners(); return "Device-rate listener failed (OSStatus \(rc))." }
            listeners.append(Listener(object: object, address: a, block: block))
        }
        return nil
    }
    func write(_ rate: Double, expected: DeviceRateSnapshot) -> String? {
        let actual = read()
        guard actual.sameDevice(expected), actual.alive, actual.nominal == expected.nominal, actual.offers(rate) else {
            return "Device identity, rate or capability changed before the request; no rate was written."
        }
        var a = address(kAudioDevicePropertyNominalSampleRate), value = rate
        let rc = AudioObjectSetPropertyData(actual.id, &a, 0, nil, UInt32(MemoryLayout<Double>.size), &value)
        return rc == noErr ? nil : "Device-rate request failed (OSStatus \(rc))."
    }
}

/// Offline backend only. A successful write deliberately does not confirm;
/// tests control readback and delivery of both current and stale callbacks.
@MainActor final class FakeDeviceRate: DeviceRateBackend {
    var snapshot = DeviceRateSnapshot()
    var writes: [Double] = []
    var writeError: String?
    var beforeWrite: (() -> Void)?
    var callbacks: [UInt64: (UInt64, Bool) -> Void] = [:]
    private(set) var observed: UInt64 = 0
    func read() -> DeviceRateSnapshot { snapshot }
    func observe(generation: UInt64, _ changed: @escaping (UInt64, Bool) -> Void) -> String? {
        observed = generation; callbacks[generation] = changed; return nil
    }
    func write(_ rate: Double, expected: DeviceRateSnapshot) -> String? {
        beforeWrite?()
        guard snapshot.sameDevice(expected), snapshot.nominal == expected.nominal, snapshot.offers(rate) else { return "Fake device changed before write." }
        if let writeError { return writeError }
        writes.append(rate); return nil
    }
    func notify(_ generation: UInt64? = nil, nominal: Bool = true) { let g = generation ?? observed; callbacks[g]?(g, nominal) }
}

// ---- helpers for the C structs ----

/// Reads a fixed C char array (imported as a tuple) as a String.
func cString<T>(_ tuple: T) -> String {
    var t = tuple
    return withUnsafeBytes(of: &t) { raw -> String in
        let bytes = raw.bindMemory(to: UInt8.self)
        let n = bytes.firstIndex(of: 0) ?? bytes.count
        return String(decoding: bytes[0..<n], as: UTF8.self)
    }
}

func opArray(_ p: PatchLines) -> [fms_op_t] { (0..<4).map { p.op($0) } }
func setOp(_ p: inout PatchLines, _ i: Int, _ op: fms_op_t) { p.setOp(i, op) }
func routeWeight(_ p: PatchLines, _ from: Int, _ to: Int) -> UInt32 { p.routeWeight(from, to) }
func setRouteWeight(_ p: inout PatchLines, _ from: Int, _ to: Int, _ w: UInt32) { p.setRouteWeight(from, to, w) }

/// Hands player pointers to the loader queue.
private struct Players: @unchecked Sendable { let list: [OpaquePointer] }

/// UI owns started; immutable player/target cross to the loader only after
/// callback retirement. Completion transfers ownership back to the UI.
private final class SeekWork: @unchecked Sendable {
    let player: OpaquePointer
    let frame: UInt32
    let epoch: Int
    var started = false
    init(_ player: OpaquePointer, _ frame: UInt32, _ epoch: Int) {
        self.player = player; self.frame = frame; self.epoch = epoch
    }
}

/// Carries a load result from the loader queue to the main thread.
private final class Loaded: @unchecked Sendable {
    var player: OpaquePointer?
    var error: String?
    var patchNotice: String?
    var initialHeader: InstrHeader?
    var info = fms_info_t()
    var events: [fms_event_t] = []
    var spacePaths: [fms_pathpt_t] = []
    var startPos: [SIMD3<Int32>] = []
    var patches: [PatchLines] = []
    var patchNames: [String] = []
    var patchDropped: [String] = []   // per patch: what its PatchLines copy loses
    var streams: [String] = []
    var voicePatch: [String] = []
    var title = ""
    var paths: [String] = []
    var live = false
}

/// One offline render (FMS_SCRIPT): the int16 frames the device would have
/// got, and whether the scope and meter buffers agree with them.
struct OfflineBlock {
    var samples: [Int16] = []
    var channels = 1
    var display: String?       // nil: fms_host_scope and fms_host_peaks equal the samples
}

/// The values the audio thread changes every block. They are published
/// here, apart from Engine, so a 30 Hz tick redraws only the views that show
/// them (Live in RecordsView.swift), not every view that observes the
/// engine . Engine alone writes them.
@MainActor final class LiveState: ObservableObject {
    @Published fileprivate(set) var positionFrames: UInt32 = 0
    @Published fileprivate(set) var runningHash: UInt64 = 0
    @Published fileprivate(set) var clipped: UInt32 = 0
    @Published fileprivate(set) var peaks: [UInt32] = []
    /// Peak hold for the meters (decays in the timer).
    @Published fileprivate(set) var peakHold: [UInt32] = []
    @Published fileprivate(set) var voiceActive: [Bool] = []
    @Published fileprivate(set) var voicePos: [SIMD3<Int32>] = []
    @Published fileprivate(set) var scope: [Int16] = []
    @Published fileprivate(set) var scopeChannels: Int = 1
}

@MainActor final class Engine: ObservableObject {
    static let shared = Engine()
    /// FMS_SCRIPT runs the app with no audio device: nothing opens the
    /// device, the 30 Hz timer does not run, and the script renders through
    /// the host callback itself (renderOffline), so every run is exact.
    nonisolated static let offline = !(ProcessInfo.processInfo.environment["FMS_SCRIPT"] ?? "").isEmpty
    /// The source node's outputs. The bridge silences records with more
    /// channels; their original channels still render, hash and visualize.
    nonisolated static let outChannels: UInt32 = 2

    @Published private(set) var info: fms_info_t?
    /// The rate the output device runs at, read after each start and on a
    /// configuration change; 0 before the first start and offline (where a
    /// script may set it with simulateDeviceRate). deviceRateLabel names it
    /// when it is not the record's rate.
    @Published private(set) var deviceRate: UInt32 = 0
    lazy var deviceControl: DeviceRateControl = {
        let backend: DeviceRateBackend = Engine.offline ? FakeDeviceRate() : HardwareDeviceRate(output: { [unowned self] in
            self.audio.outputNode.outputFormat(forBus: 0).sampleRate
        })
        let control = DeviceRateControl(backend)
        control.didRead = { [weak self] state in
            let r = state.output
            self?.deviceRate = r.isFinite && r > 0 && r <= Double(UInt32.max) ? UInt32(r.rounded()) : 0
        }
        return control
    }()
    private var rateRecoveryQueued = false
    private(set) var rateRecoveries = 0
    @Published private(set) var title: String = "Nothing loaded"
    @Published var errorText: String?
    @Published var patchNotice: String?
    @Published private(set) var initialHeader: InstrHeader?
    @Published private(set) var isPlaying = false
    @Published private(set) var isSeeking = false
    @Published private(set) var isLive = false
    @Published private(set) var isRecording = false
    /// Why the last take ended without Stop (its event log was full), or nil.
    @Published private(set) var takeEndNote: String?
    /// The take, once the audio thread has ended it (after Stop, or by itself
    /// when its log is full); InstrModel sets this.
    var onTake: ((String, Bool) -> Void)?
    @Published private(set) var events: [fms_event_t] = []
    /// Each voice's path through space (fms_player_paths), for the 3D view.
    @Published private(set) var spacePaths: [fms_pathpt_t] = []
    /// analyze.py overview's stereo columns for the loaded record, once asked
    /// for (computeStereo); its key is the loadID it was made for.
    @Published private(set) var stereo: StereoStrip?
    @Published private(set) var patches: [PatchLines] = []
    @Published private(set) var patchNames: [String] = []
    /// Per patch, parallel to patches: the statements its PatchLines copy
    /// loses (fms_player_patch_dropped), space separated; "" when exact.
    @Published private(set) var patchDropped: [String] = []
    @Published private(set) var streams: [String] = []
    @Published private(set) var voicePatch: [String] = []

    /// The per-block values. A view that shows them observes `live`
    /// (through Live); reading them here does not redraw at 30 Hz.
    let live = LiveState()
    var positionFrames: UInt32 { live.positionFrames }
    var runningHash: UInt64 { live.runningHash }
    var clipped: UInt32 { live.clipped }
    var peaks: [UInt32] { live.peaks }
    var peakHold: [UInt32] { live.peakHold }
    var voiceActive: [Bool] { live.voiceActive }
    var voicePos: [SIMD3<Int32>] { live.voicePos }
    var scope: [Int16] { live.scope }
    var scopeChannels: Int { live.scopeChannels }

    /// The paths of the loaded record (empty when live); used by verifyChunked.
    @Published private(set) var loadedPaths: [String] = []
    /// The serial of the last installed open (FMS_SCRIPT waits on it).
    @Published private(set) var installedSerial = 0

    let recordsRoot: URL
    let recipesDir: URL
    let hasSourceLibrary: Bool

    var sampleRate: UInt32 { info?.fs ?? 48000 }

    private let host: OpaquePointer
    private let loader = DispatchQueue(label: "fmspike.loader", qos: .userInitiated)
    private let analysis = DispatchQueue(label: "fmspike.analysis", qos: .utility)
    private let audio = AVAudioEngine()
    /// Offline action checks inject start failures/retries without a device.
    var audioStartOverride: (() throws -> Void)?
    private var source: AVAudioSourceNode?
    private var sourceFs: UInt32 = 0
    private let outPtrs: UnsafeMutablePointer<UnsafeMutablePointer<Float>?>
    private var current: OpaquePointer?          // the player the host runs
    private var retiring: [OpaquePointer] = []   // swapped out, waiting for fms_host_retired
    private var rewinding: OpaquePointer?        // swapped out to rewind, goes back in when retired
    private var seekWork: SeekWork?
    private var seekEpoch = 0 // record request/accepted install, never a refused live arm
    // Without a callback (offline or a stopped device), retain the sought
    // prefix display until rendering resumes; never read the worker.
    private var seekDisplay = false
    private var stopAsked = false                // Stop was pressed; tick collects the take
    /// Players swapped out and not yet closed or rewound (uitest).
    var pendingPlayers: Int { retiring.count + (rewinding == nil ? 0 : 1) + (seekWork == nil ? 0 : 1) }
    private var timer: Timer?
    private var loadSerial = 0
    /// Changes with every open; views that only show load-time data key on it.
    var loadID: Int { loadSerial }
    private var scopeBuf = [Int16](repeating: 0, count: 2048 * Int(FMS_MAX_CH))
    private var autoPlaySerial: Int?             // Explicit play belongs to one load request.

    private init() {
        // Info.plist keys from the Makefile; the environment can stand in when
        // the binary runs outside its bundle.
        let plist = Bundle.main.infoDictionary ?? [:]
        let env = ProcessInfo.processInfo.environment
        let root = (plist["FMSRepoRoot"] as? String) ?? env["FMS_REPO_ROOT"] ?? ""
        let recipes = (plist["FMSRecipesDir"] as? String) ?? env["FMS_RECIPES_DIR"] ?? ""
        let resources = Bundle.main.resourceURL ?? Bundle.main.bundleURL
        recordsRoot = root.isEmpty ? resources : URL(fileURLWithPath: root, isDirectory: true)
        recipesDir = recipes.isEmpty ? resources.appendingPathComponent("recipes", isDirectory: true)
            : URL(fileURLWithPath: recipes, isDirectory: true)
        hasSourceLibrary = !root.isEmpty
        host = fms_host_create()
        outPtrs = .allocate(capacity: Int(FMS_MAX_CH))
        outPtrs.initialize(repeating: nil, count: Int(FMS_MAX_CH))
        fms_host_set_gain_q15(host, env["FMS_MUTE"] == nil || Engine.offline ? 32768 : 0)
        // Smoke tests: FMS_OPEN=tests/golden/48000/bell.tl loads that record (relative
        // to the repository root) at launch and plays it.
        if let o = env["FMS_OPEN"], !o.isEmpty {
            open(paths: [recordsRoot.appendingPathComponent(o).path], title: o, autoplay: true)
        }
        if !Engine.offline {
            timer = Timer.scheduledTimer(withTimeInterval: 1.0 / 30.0, repeats: true) { _ in
                MainActor.assumeIsolated { Engine.shared.tick() }
            }
            _ = deviceControl
            // Notifications only refresh/recover; switching requires a click.
            NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: audio,
                                                   queue: .main) { _ in
                MainActor.assumeIsolated { Engine.shared.configurationChanged() }
            }
        }
    }

    // ---- records ----

    func listRecords() -> [RecordRef] {
        var out: [RecordRef] = []
        let fm = FileManager.default
        let golden = recordsRoot.appendingPathComponent("tests/golden")
        if let en = fm.enumerator(at: golden, includingPropertiesForKeys: nil) {
            var files: [URL] = []
            for case let u as URL in en where u.pathExtension == "tl" { files.append(u) }
            for u in files.sorted(by: { $0.path < $1.path }) {
                let rel = relative(u.path, to: recordsRoot.path)
                let group = (rel as NSString).deletingLastPathComponent
                out.append(RecordRef(id: rel, group: group, paths: [u.path]))
            }
        }
        let music = recordsRoot.appendingPathComponent("music")
        if let dirs = try? fm.contentsOfDirectory(at: music, includingPropertiesForKeys: [.isDirectoryKey]) {
            for d in dirs.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
                guard (try? d.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true else { continue }
                guard let names = try? fm.contentsOfDirectory(atPath: d.path) else { continue }
                let tls = names.filter { $0.hasSuffix(".tl") }.sorted()
                guard !tls.isEmpty else { continue }
                var pool: [String] = []
                if tls.contains("session.tl") { pool.append(d.appendingPathComponent("session.tl").path) }
                for n in tls where n != "session.tl" { pool.append(d.appendingPathComponent(n).path) }
                out.append(RecordRef(id: "music/" + d.lastPathComponent, group: "music", paths: pool))
            }
        }
        if let names = try? fm.contentsOfDirectory(atPath: recipesDir.path) {
            for n in names.filter({ $0.hasSuffix(".tl") }).sorted() {
                out.append(RecordRef(id: "recipes/" + n, group: "recipes",
                                     paths: [recipesDir.appendingPathComponent(n).path]))
            }
        }
        return out
    }

    private func relative(_ path: String, to root: String) -> String {
        let r = root.hasSuffix("/") ? root : root + "/"
        return path.hasPrefix(r) ? String(path.dropFirst(r.count)) : path
    }

    func openReadOnly(path: String, title: String) {
        open(paths: [path], title: title)
    }

    func open(_ ref: RecordRef) { open(paths: ref.paths, title: ref.title) }

    func open(paths: [String], title: String, autoplay: Bool = false) {
        seekEpoch += 1
        loadSerial += 1
        let serial = loadSerial
        autoPlaySerial = autoplay ? serial : nil
        loader.async {
            let l = Loaded()
            l.title = title
            l.paths = paths
            var err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
            let cstrs = paths.map { strdup($0) }
            defer { cstrs.forEach { free($0) } }
            let p: OpaquePointer? = cstrs.map { UnsafePointer($0) }.withUnsafeBufferPointer { buf in
                fms_player_open(buf.baseAddress, Int32(buf.count), &err, err.count)
            }
            if let p {
                l.player = p
                Engine.inspect(p, into: l)
            } else {
                l.error = String(cString: err)
            }
            DispatchQueue.main.async {
                MainActor.assumeIsolated { Engine.shared.install(l, serial: serial) }
            }
        }
    }

    /// Arms and installs a live player, then attempts playback. done(true)
    /// means it was installed; an unavailable audio device leaves it paused
    /// with a diagnostic. done(false) means it was refused or a newer open won.
    func openLive(patch: PatchLines, name: String, voices: Int, stereo: Bool, extra: String?,
                  done: @escaping @MainActor (Bool) -> Void) {
        var options = InstrSpace(); options.field = Int(stereo ? FMS_SPACE_STEREO : FMS_SPACE_OFF)
        openLive(patch: patch, name: name, voices: voices, space: options, extra: extra, done: done)
    }

    func openLive(patch: PatchLines, name: String, voices: Int, space options: InstrSpace, extra: String?,
                  geometry: [fms_event_t]? = nil, done: @escaping @MainActor (Bool) -> Void) {
        let lines = patch.text
        let nv = UInt32(max(1, min(voices, Int(FMS_MAX_VOICES))))
        let fs: UInt32 = 48000
        var space = options.native
        let need = fms_live_header_text(lines, name, nv, fs, &space, extra, nil, 0)
        guard need > 0 else {
            errorText = "live header refused: check the patch name and voice count"
            done(false)
            return
        }
        var text = [CChar](repeating: 0, count: need)
        _ = fms_live_header_text(lines, name, nv, fs, &space, extra, &text, need)
        let header = String(cString: text)
        let title = "live: " + name
        // The newest open wins, as in open(paths:).
        loadSerial += 1
        let serial = loadSerial
        autoPlaySerial = serial
        let work: () -> Loaded = {
            let l = Loaded()
            l.title = title
            l.live = true
            var err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
            if let p = fms_player_open_live(header, &err, err.count) {
                if let geometry, geometry.withUnsafeBufferPointer({
                    fms_player_seed_geometry(p, $0.baseAddress, UInt32($0.count), &err, err.count)
                }) != 0 {
                    l.error = String(cString: err); fms_player_close(p)
                } else {
                    l.player = p
                    Engine.inspect(p, into: l)
                }
            } else {
                l.error = String(cString: err)
            }
            return l
        }
        // A live header is small, but the loader queue may still be reading
        // a large pool (about 2 s), so the main thread does not wait for it
        // . Script mode opens at once: its steps stay in
        // order and no device runs.
        if Engine.offline {
            finishLive(loader.sync(execute: work), serial: serial, done: done)
        } else {
            loader.async {
                let l = work()
                DispatchQueue.main.async {
                    MainActor.assumeIsolated { Engine.shared.finishLive(l, serial: serial, done: done) }
                }
            }
        }
    }

    private func finishLive(_ l: Loaded, serial: Int, done: @MainActor (Bool) -> Void) {
        guard l.player != nil else {
            // Refused: the player that runs keeps running.
            installedSerial = max(installedSerial, serial)
            if serial == loadSerial {
                autoPlaySerial = nil
                errorText = "live: " + (l.error ?? "refused")
            }
            done(false)
            return
        }
        let won = serial == loadSerial
        install(l, serial: serial)   // closes it when a newer open won
        // install owns the request's autoplay, which a later local import can cancel.
        done(won)
    }

    nonisolated private static func inspect(_ p: OpaquePointer, into l: Loaded) {
        var info = fms_info_t()
        fms_player_info(p, &info)
        l.info = info
        l.initialHeader = InstrHeader.read(p)
        if !l.live {
            let n = fms_player_events(p, nil, 0)
            var ev = [fms_event_t](repeating: fms_event_t(), count: Int(n))
            if n > 0 { _ = fms_player_events(p, &ev, n) }
            l.events = ev
            let np = fms_player_paths(p, nil, 0)
            var pts = [fms_pathpt_t](repeating: fms_pathpt_t(), count: Int(np))
            if np > 0 { _ = fms_player_paths(p, &pts, np) }
            l.spacePaths = pts
        }
        let nv = fms_player_voice_pos(p, nil, 0)
        var xyz = [Int32](repeating: 0, count: Int(nv) * 3)
        if nv > 0 { _ = fms_player_voice_pos(p, &xyz, nv) }
        l.startPos = (0..<Int(nv)).map { SIMD3(xyz[$0 * 3], xyz[$0 * 3 + 1], xyz[$0 * 3 + 2]) }
        for i in 0..<info.npatches {
            if let pt = PatchLines.read(p, i, fs: info.fs) {
                l.patches.append(pt)
                l.patchNames.append(pt.name)
                l.patchDropped.append(instrDropped(p, i))
            }
        }
        for i in 0..<info.nstreams {
            l.streams.append(fms_player_stream_name(p, i).map { String(cString: $0) } ?? "s\(i)")
        }
        for v in 0..<info.nvoices {
            l.voicePatch.append(fms_player_voice_patch(p, v).map { String(cString: $0) } ?? "?")
        }
    }

    /// Installs a load result; a refused record unloads the current player
    /// and shows the harness's diagnostics instead.
    private func install(_ l: Loaded, serial: Int) {
        installedSerial = max(installedSerial, serial)
        if serial != loadSerial {   // a newer open won: drop this one
            if let p = l.player {
                let box = Players(list: [p])
                loader.async { box.list.forEach { fms_player_close($0) } }
            }
            return
        }
        seekEpoch += 1
        if isRecording { abandonTake() }
        if let r = rewinding { retiring.append(r); rewinding = nil }
        if let work = seekWork, !work.started {
            retiring.append(work.player); seekWork = nil; isSeeking = false
        }
        seekDisplay = false
        fms_host_set_playing(host, 0)
        isPlaying = false
        let p = l.player
        errorText = p == nil ? (l.error ?? "refused") : nil
        if p != nil, l.info.fs != sourceFs || source == nil { rebuildAudio(fs: l.info.fs) }
        if let old = fms_host_swap(host, p) { retiring.append(old) }
        current = p
#if FMS_RATE_MUTANT
        // Deliberate no-click regression, compiled only for the offline control.
        if Engine.offline, p != nil { _ = requestDeviceRate(Double(l.info.fs)) }
#endif
        info = p == nil ? nil : l.info
        title = l.title
        isLive = l.live
        events = l.events
        spacePaths = l.spacePaths
        stereo = nil
        patches = l.patches
        patchNames = l.patchNames
        patchDropped = l.patchDropped
        patchNotice = l.patchNotice
        initialHeader = l.initialHeader
        streams = l.streams
        voicePatch = l.voicePatch
        loadedPaths = l.live ? [] : l.paths
        live.positionFrames = 0
        live.runningHash = 0
        live.clipped = 0
        let ch = Int(max(1, l.info.channels))
        live.peaks = Array(repeating: 0, count: ch)
        live.peakHold = live.peaks
        live.voiceActive = Array(repeating: false, count: Int(l.info.nvoices))
        live.voicePos = l.startPos.count == Int(l.info.nvoices) ? l.startPos
            : Array(repeating: SIMD3<Int32>(0, 0, 0), count: Int(l.info.nvoices))
        live.scope = []
        live.scopeChannels = ch
        let requestedPlay = autoPlaySerial == serial
        if requestedPlay { autoPlaySerial = nil }
        if requestedPlay, p != nil { play() }
    }

    // ---- audio ----

    nonisolated private static func makeSource(host: OpaquePointer, format: AVAudioFormat,
                                               ptrs: UnsafeMutablePointer<UnsafeMutablePointer<Float>?>) -> AVAudioSourceNode {
        // The render block captures only the two pointers: it never allocates.
        AVAudioSourceNode(format: format) { _, _, frameCount, abl -> OSStatus in
            let list = UnsafeMutableAudioBufferListPointer(abl)
            var n = 0
            for b in list where n < Int(FMS_MAX_CH) {
                ptrs[n] = b.mData?.assumingMemoryBound(to: Float.self)
                n += 1
            }
            fms_host_render_f32(host, ptrs, UInt32(n), frameCount)
            return noErr
        }
    }

    private func rebuildAudio(fs: UInt32) {
        if Engine.offline { sourceFs = fs; _ = startAudio(); return }   // no device
        audio.stop()
        if let s = source {
            audio.disconnectNodeOutput(s)
            audio.detach(s)
            source = nil
        }
        guard let fmt = AVAudioFormat(standardFormatWithSampleRate: Double(fs), channels: Engine.outChannels) else {
            stop()
            errorText = "audio: no audio format at \(fs) Hz"
            return
        }
        let s = Engine.makeSource(host: host, format: fmt, ptrs: outPtrs)
        audio.attach(s)
        audio.connect(s, to: audio.mainMixerNode, format: fmt)
        source = s
        sourceFs = fs
        _ = startAudio()
    }

    /// Start failure pauses the transport without discarding its player.
    /// A successful retry clears an earlier audio diagnostic only.
    private func startAudio() -> Bool {
        do {
            if let audioStartOverride { try audioStartOverride() }
            else if !Engine.offline && !audio.isRunning { try audio.start() }
            if errorText?.hasPrefix("audio: ") == true { errorText = nil }
            if !Engine.offline { readDeviceRate() }
            return true
        } catch {
            stop()
            errorText = "audio: \(error.localizedDescription)"
            if !Engine.offline { readDeviceRate() }
            return false
        }
    }

    /// Read actual rates without requesting a change.
    func readDeviceRate() { deviceControl.refresh() }

    /// A HAL or graph change never changes the integer player or source rate.
    /// Defer graph recovery until after AVAudioEngine's notification returns.
    func configurationChanged() {
        readDeviceRate()
        guard !rateRecoveryQueued else { return }
        rateRecoveryQueued = true
        Task { @MainActor [weak self] in
            await Task.yield()
            guard let self else { return }
            defer { self.rateRecoveryQueued = false }
            guard self.current != nil, self.sourceFs > 0 else { return }
            // A delayed notification from an already repaired graph must not
            // stop it again. Offline controls exercise the same coalescing.
            guard Engine.offline || !self.audio.isRunning else { return }
            self.rateRecoveries += 1
            self.rebuildAudio(fs: self.sourceFs)
            self.readDeviceRate()
        }
    }

    @discardableResult func requestDeviceRate(_ rate: Double, restore: Bool = false) -> String? {
        let error = restore ? deviceControl.restore() : deviceControl.request(rate)
        guard error == nil else { return error }
        let token = deviceControl.generation
        if !Engine.offline {
            Task { @MainActor [weak self] in
                try? await Task.sleep(for: .seconds(5))
                self?.deviceControl.timeout(token)
            }
        }
        return nil
    }

    /// Script mode never creates the hardware backend or opens a device.
    func simulateDeviceRate(_ fs: UInt32) {
        guard Engine.offline, let fake = deviceControl.backend as? FakeDeviceRate else { return }
        fake.snapshot = DeviceRateSnapshot(id: fs == 0 ? 0 : 1, uid: fs == 0 ? "" : "offline",
                                          alive: fs != 0, nominal: Double(fs), output: Double(fs),
                                          ranges: [8000...192000], settable: fs != 0)
        deviceControl.refresh()
    }

    func play() {
        guard current != nil else { return }
        if !Engine.offline, let info, info.channels > Engine.outChannels {
            stop()
            errorText = "Raw field playback needs a stereo or binaural record. All source channels remain available for visualization and export."
            return
        }
        guard startAudio() else { return }
        fms_host_set_playing(host, 1)
        isPlaying = true
    }

    func stop() {
        fms_host_set_playing(host, 0)
        isPlaying = false
    }

    /// A local file choice pauses both the current and any pending explicit play.
    /// Its verification can finish while that older load installs silently.
    func pauseForImport() {
        autoPlaySerial = nil
        stop()
    }

    /// Swaps the player out, rewinds it once the audio thread lets go (see tick), swaps it back.
    func rewind() {
        guard let p = current, rewinding == nil, !isSeeking else { return }
        if isRecording { abandonTake() }
        _ = fms_host_swap(host, nil)
        rewinding = p
        seekDisplay = false
        if errorText?.hasPrefix("Generator render refused:") == true { errorText = nil }
        live.positionFrames = 0
        live.runningHash = 0
        live.clipped = 0
    }

    /// Records only. The callback first retires the player; tick starts the
    /// prefix on the loader. Playback/pause selection is not changed.
    @discardableResult func seek(_ frame: UInt32) -> String? {
        // The script's binding-removal control, as for the other UI setters.
        if Engine.offline && ProcessInfo.processInfo.environment["FMS_UITEST_DROP"] == "record.seek" { return nil }
        guard let p = current, !isLive, let info, frame <= info.frames else {
            return "Seek needs a loaded record frame from zero through its length."
        }
        guard !isSeeking, rewinding == nil else { return "A seek or rewind is already pending." }
        _ = fms_host_swap(host, nil)
        seekWork = SeekWork(p, frame, seekEpoch)
        isSeeking = true
        seekDisplay = false
        return nil
    }

    private func startSeek(_ work: SeekWork) {
        work.started = true
        loader.async {
            var error = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
            let rc = fms_player_seek(work.player, work.frame, &error, error.count)
            let message = rc == 0 ? nil : String(cString: error)
            let position = fms_player_position(work.player)
            let hash = fms_player_hash(work.player)
            let clips = fms_player_clipped(work.player)
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    let engine = Engine.shared
                    precondition(engine.seekWork === work)
                    engine.seekWork = nil; engine.isSeeking = false
                    if engine.current == work.player && engine.seekEpoch == work.epoch {
                        _ = fms_host_swap(engine.host, work.player)
                        engine.live.positionFrames = position
                        engine.live.runningHash = hash
                        engine.live.clipped = clips
                        engine.live.scope = []
                        engine.live.peaks = engine.live.peaks.map { _ in 0 }
                        engine.live.peakHold = engine.live.peaks
                        engine.seekDisplay = engine.noCallback
                        if let message { engine.errorText = message; engine.stop() }
                    } else {
                        // A newer open owns the transport. The retired source
                        // was never reinstalled and is closed exactly once.
                        if engine.current == work.player { engine.current = nil }
                        let box = Players(list: [work.player])
                        engine.loader.async { box.list.forEach { fms_player_close($0) } }
                    }
                }
            }
        }
    }

    /// false when the live queue was full and the event was dropped.
    @discardableResult func push(_ e: fms_event_t) -> Bool {
        var ev = e
        if fms_host_push(host, &ev) != 0 { errorText = "live queue full"; return false }
        return true
    }

    /// The piano's note-on and note-off; live MIDI input's pool hears of
    /// the voice (MidiInput.external).
    func noteOn(voice: Int, freqMilliHz: UInt32, velocityQ15: UInt32) {
        var e = fms_event_t()
        e.type = UInt32(FMS_EV_ON)
        e.voice = UInt32(voice)
        e.freq_millihz = freqMilliHz
        e.value = velocityQ15
        push(e)
        MidiInput.shared.external(voice: voice, on: true)
    }

    func noteOff(voice: Int) {
        var e = fms_event_t()
        e.type = UInt32(FMS_EV_OFF)
        e.voice = UInt32(voice)
        push(e)
        MidiInput.shared.external(voice: voice, on: false)
    }

    @discardableResult
    func startRecording() -> Bool {
        guard isLive, current != nil, !isRecording, fms_host_recording(host) == 0 else {
            if !isLive || current == nil { errorText = "recording needs a live player" }
            return false
        }
        fms_host_record_start(host)
        guard fms_host_recording(host) != 0 else { return false }
        isRecording = true
        stopAsked = false
        takeEndNote = nil
        if !isPlaying { play() }
        return true
    }

    /// Asks the audio thread to end the take and returns at once; the take
    /// reaches onTake when it has (tick), or here when no callback runs.
    func stopRecording() {
        guard isRecording else { return }
        fms_host_record_request_stop(host)
        stopAsked = true
        if noCallback { fms_host_settle(host); collectTake() }
    }

    /// Opening another record or rewinding while recording drops the take,
    /// as before; the swap ends it on the audio thread.
    private func abandonTake() {
        fms_host_record_request_stop(host)
        isRecording = false
        stopAsked = false
    }

    /// No audio callback can run: offline, or the device is not running.
    private var noCallback: Bool { Engine.offline || !audio.isRunning }

    /// Once the audio thread has ended the take (Stop, or a full log): the
    /// Record button comes back and the take reaches onTake.
    private func collectTake() {
        guard isRecording, fms_host_recording(host) == 0 else { return }
        // Snapshot the authoritative outcome while this serialized UI still
        // owns the ended request, before another Record can reset it.
        let started = fms_host_record_started(host) != 0
        isRecording = false
        takeEndNote = !stopAsked && fms_host_take_full(host) != 0
            ? "The take ended by itself: its event log is full (65536 events)." : nil
        stopAsked = false
        let need = fms_host_take_text(host, nil, 0)
        var text = ""
        if need > 0 {
            var buf = [CChar](repeating: 0, count: need)
            _ = fms_host_take_text(host, &buf, need)
            text = String(cString: buf)
        }
        onTake?(text, started)
    }

    /// Renders the loaded record offline in a separate player at several chunk
    /// sizes and compares every hash with the record's. Replaced-load results
    /// are discarded; a completion always belongs to the captured record.
    func verifyChunked(_ done: @escaping (UInt64, Bool, String) -> Void) {
        let paths = loadedPaths
        guard !paths.isEmpty else { done(0, false, "nothing to verify (live or empty)"); return }
        let request = loadID, installed = installedSerial
        let expected = info?.have_hash != 0 ? info?.hash_expected : nil
        loader.async {
            var err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
            let cstrs = paths.map { strdup($0) }
            defer { cstrs.forEach { free($0) } }
            let p: OpaquePointer? = cstrs.map { UnsafePointer($0) }.withUnsafeBufferPointer { buf in
                fms_player_open(buf.baseAddress, Int32(buf.count), &err, err.count)
            }
            var hash: UInt64 = 0
            var ok = false
            var summary = ""
            if let p {
                var parts: [String] = []
                var hashes: Set<UInt64> = []
                let t0 = Date()
                var activeChunk: UInt32 = 0
                defer { fms_player_close(p) }
                do {
                    for chunk: UInt32 in [0, 1, 37, 256, 4096] {
                        activeChunk = chunk
                        let (h, _) = try InstrPatchIO.renderChecked(p, chunk: chunk)
                        hashes.insert(h)
                        hash = h
                        parts.append("\(chunk == 0 ? "all" : String(chunk)):\(String(format: "%016llx", h))")
                    }
                    let same = hashes.count == 1
                    let vsExpected: String
                    if let e = expected {
                        ok = same && hash == e
                        vsExpected = hash == e ? "equals the record's hash" : String(format: "differs from the record's %016llx", e)
                    } else {
                        ok = same
                        vsExpected = "record has no hash statement"
                    }
                    summary = "\(same ? "all chunk sizes agree" : "CHUNK SIZES DISAGREE"); \(vsExpected) "
                        + String(format: "(%.2f s)\n", Date().timeIntervalSince(t0)) + parts.joined(separator: "  ")
                } catch {
                    hash = 0
                    summary = "render refused at chunk \(activeChunk == 0 ? "all" : String(activeChunk)): \(error)"
                }
            } else {
                summary = String(cString: err)
            }
            let (h, o, s) = (hash, ok, summary)
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    let engine = Engine.shared
                    guard engine.loadID == request, engine.installedSerial == installed else { return }
                    done(h, o, s)
                }
            }
        }
    }

    // ---- the stereo picture  ----

    /// analyze.py overview's side-mid and corr per window over the loaded
    /// record: a second player renders it to its end on the analysis queue.
    /// Two-channel records only (nil otherwise). windowFrames 0: analyze.py's
    /// default window, 4 s. keepSamples hands the rendered frames to done.
    func computeStereo(windowFrames: UInt32 = 0, keepSamples: Bool = false,
                       _ done: ((StereoStrip?, [Int16]) -> Void)? = nil) {
        guard let inf = info, inf.channels == 2, !isLive, !loadedPaths.isEmpty else {
            stereo = nil
            done?(nil, [])
            return
        }
        let key = loadID, paths = loadedPaths
        let w = windowFrames > 0 ? windowFrames : inf.fs * 4
        if done == nil, let st = stereo, st.key == key, st.window == w { return }
        analysis.async {
            let (st, kept) = Engine.stereoPass(paths: paths, key: key, window: w, keep: keepSamples)
            DispatchQueue.main.async {
                if key == self.loadID { self.stereo = st }
                done?(st, kept)
            }
        }
    }

    nonisolated private static func stereoPass(paths: [String], key: Int, window: UInt32, keep: Bool) -> (StereoStrip, [Int16]) {
        var err = [CChar](repeating: 0, count: Int(FMS_ERR_LEN))
        let cstrs = paths.map { strdup($0) }
        defer { cstrs.forEach { free($0) } }
        let opened: OpaquePointer? = cstrs.map { UnsafePointer($0) }.withUnsafeBufferPointer { buf in
            fms_player_open(buf.baseAddress, Int32(buf.count), &err, err.count)
        }
        guard let p = opened else {
            return (StereoStrip(key: key, window: window, fs: 0, frames: 0, rows: [], hash: 0, error: String(cString: err)), [])
        }
        defer { fms_player_close(p) }
        fms_player_rewind(p)
        var inf = fms_info_t()
        fms_player_info(p, &inf)
        let width = Int(max(window, 1))
        let count = max(1, Int(inf.frames) / width)
        var sums = [StereoSums](repeating: StereoSums(), count: count)
        var kept: [Int16] = []
        if keep { kept.reserveCapacity(Int(inf.frames) * 2) }
        let chunk = 8192
        var buf = [Int16](repeating: 0, count: chunk * 2)
        var frame = 0
        while true {
            let got = Int(buf.withUnsafeMutableBufferPointer { fms_player_render(p, $0.baseAddress, UInt32(chunk)) })
            if got == 0 { break }
            for i in 0..<got {
                // : the final row includes the remainder; a short record
                // still has one row containing all of its frames.
                let k = min(count - 1, (frame + i) / width)
                sums[k].add(buf[2 * i], buf[2 * i + 1])
            }
            if keep { kept.append(contentsOf: buf[0..<(2 * got)]) }
            frame += got
        }
        let code = fms_player_error(p)
        let rows = sums.enumerated().map { index, sum in
            StereoRow(start: UInt32(index * width), length: UInt32(sum.n), sideMid: sum.sideMid, corr: sum.corr)
        }
        let st = StereoStrip(key: key, window: window, fs: inf.fs, frames: UInt32(frame), rows: rows, hash: fms_player_hash(p),
                             error: code != 0 ? "render refused: code \(code & 255)" : nil)
        return (st, kept)
    }

    // ---- offline (FMS_SCRIPT) ----

    /// Renders frames through fms_host_render_f32, as the device's callback
    /// would, in calls of 471 frames; then reads the scope and the meters back
    /// and compares them with the samples, and runs the timer's work once.
    func renderOffline(_ frames: Int) -> OfflineBlock {
        precondition(Engine.offline)
        if frames > 0 { seekDisplay = false }
        var out = OfflineBlock()
        let ch = Int(max(1, min(info?.channels ?? 1, UInt32(FMS_MAX_CH))))
        out.channels = ch
        out.samples = [Int16](repeating: 0, count: frames * ch)
        let call = 471
        var bufs = (0..<ch).map { _ in [Float](repeating: 0, count: call) }
        var done = 0
        while done < frames {
            let n = min(call, frames - done)
            for c in 0..<ch {
                bufs[c].withUnsafeMutableBufferPointer { outPtrs[c] = $0.baseAddress }
            }
            fms_host_render_f32(host, outPtrs, UInt32(ch), UInt32(n))
            for c in 0..<ch {
                for i in 0..<n {   // the gain is unity offline: f * 32768 is the int16 exactly
                    out.samples[(done + i) * ch + c] = Int16(bufs[c][i] * 32768)
                }
            }
            done += n
        }
        for c in 0..<ch { outPtrs[c] = nil }
        if isPlaying && frames > 0 {
            var pk = [UInt32](repeating: 0, count: ch)
            fms_host_peaks(host, &pk, UInt32(ch))
            for c in 0..<ch {
                var m: UInt32 = 0
                for i in 0..<frames { m = max(m, UInt32(abs(Int32(out.samples[i * ch + c])))) }
                if m != pk[c] { out.display = "meter ch \(c): \(pk[c]) against the samples' \(m)" }
            }
            let n = min(frames, 8192)
            var sc = [Int16](repeating: 0, count: n * Int(FMS_MAX_CH))
            var sch: UInt32 = 0
            let got = Int(fms_host_scope(host, &sc, UInt32(n), &sch))
            if Int(sch) != ch || got != n
                || Array(sc[0..<(n * ch)]) != Array(out.samples[((frames - n) * ch)...]) {
                out.display = (out.display ?? "") + " scope differs from the last \(n) frames"
            }
        }
        tick()
        return out
    }

    // ---- the 30 Hz timer ----

    private var profileRefusals: UInt32 = 0

    private func tick() {
        // With no callback running (the device failed to start or stopped),
        // nothing else would retire a swapped-out player or end a take.
        if noCallback { fms_host_settle(host) }
        collectTake()
        let refused = fms_host_profile_refusals(host)
        if refused != profileRefusals {
            profileRefusals = refused
            errorText = "Position event refused by the armed decodable profile: only one fixed position per voice at frame zero is allowed. Queue acceptance is not event application."
        }
        // Close players the audio thread has let go of.
        if fms_host_retired(host) != 0 {
            if let work = seekWork, !work.started { startSeek(work) }
            if let r = rewinding {
                loader.sync { fms_player_rewind(r) }
                rewinding = nil
                if r == current { _ = fms_host_swap(host, r) } else { retiring.append(r) }
            }
            if !retiring.isEmpty, fms_host_retired(host) != 0 {
                let box = Players(list: retiring)
                retiring = []
                loader.async { box.list.forEach { fms_player_close($0) } }
            }
        }
        guard let inf = info, current != nil else { return }
        if rewinding != nil || seekWork?.player == current || (seekDisplay && noCallback) { return }
        seekDisplay = false
        let pos = fms_host_position(host)
        if pos != live.positionFrames { live.positionFrames = pos }
        let h = fms_host_hash(host)
        if h != live.runningHash { live.runningHash = h }
        let c = fms_host_clipped(host)
        if c != live.clipped { live.clipped = c }
        let renderError = fms_host_error(host)
        if renderError != 0 && fms_host_retired(host) != 0 {
            errorText = "Generator render refused: code \(renderError & 255), ID \((renderError >> 8) & 255), field \((renderError >> 16) & 255)."
            fms_host_set_playing(host, 0)
            isPlaying = false
            return
        }
        guard isPlaying else { return }

        let ch = Int(max(1, min(inf.channels, UInt32(FMS_MAX_CH))))
        var pk = [UInt32](repeating: 0, count: ch)
        fms_host_peaks(host, &pk, UInt32(ch))
        if pk != live.peaks { live.peaks = pk }
        var hold = live.peakHold.count == ch ? live.peakHold : pk
        for i in 0..<ch { hold[i] = max(pk[i], hold[i] &* 15 / 16) }
        if hold != live.peakHold { live.peakHold = hold }

        let nv = Int(inf.nvoices)
        if nv > 0 {
            var act = [UInt8](repeating: 0, count: nv)
            _ = fms_host_voice_active(host, &act, UInt32(nv))
            let on = act.map { $0 != 0 }
            if on != live.voiceActive { live.voiceActive = on }
            if inf.space != 0 || inf.route != 0 {
                var xyz = [Int32](repeating: 0, count: nv * 3)
                fms_host_voice_pos(host, &xyz, UInt32(nv))
                let at = (0..<nv).map { SIMD3(xyz[$0 * 3], xyz[$0 * 3 + 1], xyz[$0 * 3 + 2]) }
                if at != live.voicePos { live.voicePos = at }
            }
        }

        var sch: UInt32 = 0
        let frames: UInt32 = 1024
        let got = scopeBuf.withUnsafeMutableBufferPointer { buf in
            fms_host_scope(host, buf.baseAddress, frames, &sch)
        }
        let sc = Int(max(1, sch))
        let frame = Array(scopeBuf[0..<min(scopeBuf.count, Int(got) * sc)])
        if frame != live.scope { live.scope = frame }
        if sc != live.scopeChannels { live.scopeChannels = sc }
    }
}
