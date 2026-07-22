// ocr.swift — scanned-page OCR via macOS Vision framework.
// Usage: ocr <image-path> [language]     (default ja-JP; en-US always added)
//        ocr --list-langs                (JSON list of supported languages)
// Prints JSON: [{"text": "...", "x": px, "y": px, "w": px, "h": px,
//                "conf": 0.98, "chars": [{"c": "字", "x":..,"y":..,"w":..,"h":..}, ...]}]
// Coordinates are pixels, origin top-left.

import Foundation
import Vision
import CoreGraphics
import ImageIO

if CommandLine.arguments.count == 2 && CommandLine.arguments[1] == "--list-langs" {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    let langs = (try? req.supportedRecognitionLanguages()) ?? []
    FileHandle.standardOutput.write(
        try! JSONSerialization.data(withJSONObject: langs))
    exit(0)
}

guard CommandLine.arguments.count >= 2 else {
    FileHandle.standardError.write(
        "usage: ocr <image-path> [language] | ocr --list-langs\n".data(using: .utf8)!)
    exit(2)
}
let path = CommandLine.arguments[1]
let primaryLang = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : "ja-JP"
let url = URL(fileURLWithPath: path)

guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
      let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
    FileHandle.standardError.write("cannot read image: \(path)\n".data(using: .utf8)!)
    exit(1)
}
let W = CGFloat(img.width), H = CGFloat(img.height)

func pixelRect(_ bb: CGRect) -> [String: Double] {
    return ["x": Double(bb.origin.x * W),
            "y": Double((1 - bb.origin.y - bb.size.height) * H),
            "w": Double(bb.size.width * W),
            "h": Double(bb.size.height * H)]
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = primaryLang == "en-US"
    ? ["en-US"] : [primaryLang, "en-US"]
request.usesLanguageCorrection = true

let handler = VNImageRequestHandler(cgImage: img, options: [:])
do {
    try handler.perform([request])
} catch {
    FileHandle.standardError.write("OCR failed: \(error)\n".data(using: .utf8)!)
    exit(1)
}

var out: [[String: Any]] = []
for obs in request.results ?? [] {
    guard let cand = obs.topCandidates(1).first else { continue }
    let s = cand.string
    var entry: [String: Any] = ["text": s, "conf": Double(cand.confidence)]
    let lineRect = pixelRect(obs.boundingBox)
    for (k, v) in lineRect { entry[k] = v }

    var chars: [[String: Any]] = []
    var idx = s.startIndex
    while idx < s.endIndex {
        let next = s.index(after: idx)
        var cbox = lineRect
        if let rectObs = try? cand.boundingBox(for: idx..<next) {
            cbox = pixelRect(rectObs.boundingBox)
        }
        var c: [String: Any] = ["c": String(s[idx..<next])]
        for (k, v) in cbox { c[k] = v }
        chars.append(c)
        idx = next
    }
    entry["chars"] = chars
    out.append(entry)
}

let data = try! JSONSerialization.data(withJSONObject: out, options: [])
FileHandle.standardOutput.write(data)
