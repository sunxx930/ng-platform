// macOS Vision OCR：把图片上的文字识别成文本（用于扫描件/坏字体 PDF）。
// 用法:
//   ocr_mac <图片路径>...            —— 逐张识别，按行输出纯文本
//   ocr_mac --coords <图片路径>...   —— 每行输出「x0 y0 x1 y1\t文本」（归一化 0-1，原点左下）
// 无需安装任何东西，用系统自带的 Vision 框架（支持简体/繁体中文 + 英文）。
import Foundation
import Vision
import AppKit

var args = Array(CommandLine.arguments.dropFirst())
let wantCoords = args.first == "--coords"
if wantCoords { args.removeFirst() }
if args.isEmpty {
    FileHandle.standardError.write("用法: ocr_mac [--coords] <图片路径>...\n".data(using: .utf8)!)
    exit(2)
}
for path in args {
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
        guard let c = obs.topCandidates(1).first else { continue }
        if wantCoords {
            let b = obs.boundingBox            // 归一化，原点左下
            print("\(b.minX) \(b.minY) \(b.maxX) \(b.maxY)\t\(c.string)")
        } else {
            print(c.string)
        }
    }
}
