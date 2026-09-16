import Foundation
import Vision
import AppKit
// 用法: ocr <image...>  → 每行一段识别文字（zh-Hans + en）
for path in CommandLine.arguments.dropFirst() {
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { continue }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.recognitionLanguages = ["zh-Hans", "en-US"]
    req.usesLanguageCorrection = true
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    try? handler.perform([req])
    for obs in (req.results ?? []) {
        if let c = obs.topCandidates(1).first { print(c.string) }
    }
}
