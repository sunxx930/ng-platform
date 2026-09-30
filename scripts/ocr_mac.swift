// macOS Vision OCR：把图片上的文字识别成文本（用于扫描件 PDF）。
// 用法: ocr_mac <图片路径>...   —— 逐张识别，按行输出到 stdout。
// 无需安装任何东西，用系统自带的 Vision 框架（支持简体/繁体中文 + 英文）。
import Foundation
import Vision
import AppKit

let paths = Array(CommandLine.arguments.dropFirst())
if paths.isEmpty {
    FileHandle.standardError.write("用法: ocr_mac <图片路径>...\n".data(using: .utf8)!)
    exit(2)
}
for path in paths {
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        FileHandle.standardError.write("无法读取图片: \(path)\n".data(using: .utf8)!)
        continue
    }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
    req.usesLanguageCorrection = true
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    do {
        try handler.perform([req])
    } catch {
        FileHandle.standardError.write("识别失败: \(path)\n".data(using: .utf8)!)
        continue
    }
    for obs in (req.results ?? []) {
        if let c = obs.topCandidates(1).first { print(c.string) }
    }
}
