// translate.swift — offline translation via Apple's Translation framework
// (macOS 15+; language packs download once per pair).
//
// Usage:
//   echo '{"source":"ja","target":"en","texts":["…"]}' | ./translate
//     -> JSON array of translated strings (same order), exit 0.
//        Exits fast with code 4 if the language pack isn't installed —
//        never prompts, so headless callers can fall back instantly.
//   ./translate --list-langs            JSON array of language ids
//   ./translate --status  <src> <tgt>   "installed" | "supported" | "unsupported"
//   ./translate --prepare <src> <tgt>   VISIBLE window prompting the user to
//                                       download the pack; exit 0 when done
//
// The TranslationSession API only hands out sessions through the SwiftUI
// .translationTask modifier, so translation runs inside an invisible 1x1
// window pumping the app loop; --prepare uses a real titled window.

import AppKit
import SwiftUI
import Translation

struct Job: Decodable {
    let source: String
    let target: String
    let texts: [String]
}

func fail(_ msg: String, code: Int32 = 1) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(code)
}

func statusName(_ s: LanguageAvailability.Status) -> String {
    switch s {
    case .installed:   return "installed"
    case .supported:   return "supported"
    default:           return "unsupported"
    }
}

let args = CommandLine.arguments
var window: NSWindow?          // retained for the app-loop lifetime

// ------------------------------------------------------- list languages

if args.contains("--list-langs") {
    Task {
        let langs = await LanguageAvailability().supportedLanguages
        let ids = Set(langs.map { $0.languageCode?.identifier ?? "" })
            .filter { !$0.isEmpty }.sorted()
        FileHandle.standardOutput.write(
            try! JSONSerialization.data(withJSONObject: ids))
        exit(0)
    }
    RunLoop.main.run()
}

// ------------------------------------------------------------- status

if let i = args.firstIndex(of: "--status") {
    guard args.count > i + 2 else { fail("usage: --status <src> <tgt>", code: 2) }
    let s = Locale.Language(identifier: args[i + 1])
    let t = Locale.Language(identifier: args[i + 2])
    Task {
        print(statusName(await LanguageAvailability().status(from: s, to: t)))
        exit(0)
    }
    RunLoop.main.run()
}

// ------------------------------------------------- prepare (download UI)

struct PrepareView: View {
    let source: String, target: String
    @State private var config: TranslationSession.Configuration?

    var body: some View {
        Text("Approve the language download in the dialog…")
            .frame(width: 360, height: 90)
            .translationTask(config) { session in
                do {
                    try await session.prepareTranslation()
                    print("ok")
                    exit(0)
                } catch {
                    fail("prepare failed: \(error)")
                }
            }
            .onAppear {
                config = TranslationSession.Configuration(
                    source: Locale.Language(identifier: source),
                    target: Locale.Language(identifier: target))
            }
    }
}

if let i = args.firstIndex(of: "--prepare") {
    guard args.count > i + 2 else { fail("usage: --prepare <src> <tgt>", code: 2) }
    let app = NSApplication.shared
    app.setActivationPolicy(.regular)
    let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 360, height: 90),
                     styleMask: [.titled, .closable], backing: .buffered,
                     defer: false)
    w.title = "Download translation languages"
    w.contentView = NSHostingView(
        rootView: PrepareView(source: args[i + 1], target: args[i + 2]))
    w.center()
    w.makeKeyAndOrderFront(nil)
    window = w
    app.activate(ignoringOtherApps: true)
    DispatchQueue.main.asyncAfter(deadline: .now() + 600) {
        fail("prepare timed out", code: 3)
    }
    app.run()
}

// ----------------------------------------------------------- translate

let input = FileHandle.standardInput.readDataToEndOfFile()
guard let job = try? JSONDecoder().decode(Job.self, from: input) else {
    fail("stdin must be JSON {\"source\":..,\"target\":..,\"texts\":[..]}", code: 2)
}
if job.texts.isEmpty {
    FileHandle.standardOutput.write("[]".data(using: .utf8)!)
    exit(0)
}

struct TranslatorView: View {
    let job: Job
    @State private var config: TranslationSession.Configuration?

    var body: some View {
        Color.clear
            .translationTask(config) { session in
                do {
                    let reqs = job.texts.enumerated().map {
                        TranslationSession.Request(sourceText: $0.element,
                                                   clientIdentifier: String($0.offset))
                    }
                    let responses = try await session.translations(from: reqs)
                    var out = [String](repeating: "", count: job.texts.count)
                    for r in responses {
                        if let i = Int(r.clientIdentifier ?? "") { out[i] = r.targetText }
                    }
                    FileHandle.standardOutput.write(
                        try JSONSerialization.data(withJSONObject: out))
                    exit(0)
                } catch {
                    fail("translation failed: \(error)")
                }
            }
            .onAppear {
                config = TranslationSession.Configuration(
                    source: Locale.Language(identifier: job.source),
                    target: Locale.Language(identifier: job.target))
            }
    }
}

// safety net: never hang the caller
DispatchQueue.main.asyncAfter(deadline: .now() + 180) {
    fail("translation timed out", code: 3)
}

let app = NSApplication.shared
app.setActivationPolicy(.prohibited)      // no Dock icon, no focus steal
Task { @MainActor in
    // Refuse to start a session that would need a language download — a
    // headless helper can't host that prompt (it hangs as a blank window).
    let st = await LanguageAvailability().status(
        from: Locale.Language(identifier: job.source),
        to: Locale.Language(identifier: job.target))
    guard st == .installed else {
        fail(st == .supported
             ? "language pack \(job.source)->\(job.target) not downloaded — "
               + "use Settings > Download language pack"
             : "language pair \(job.source)->\(job.target) unsupported",
             code: 4)
    }
    let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1, height: 1),
                     styleMask: [.borderless], backing: .buffered, defer: false)
    w.alphaValue = 0
    w.contentView = NSHostingView(rootView: TranslatorView(job: job))
    w.orderFrontRegardless()
    window = w
}
app.run()
