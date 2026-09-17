import Foundation
import PDFKit
import Vision
import AppKit
// 用法: pdfocr <file.pdf...> → 每页渲染成图后 OCR（zh-Hans+en）
for path in CommandLine.arguments.dropFirst() {
    guard let doc = PDFDocument(url: URL(fileURLWithPath: path)) else { continue }
    for i in 0..<doc.pageCount {
        guard let page = doc.page(at: i) else { continue }
        let b = page.bounds(for: .mediaBox)
        let size = NSSize(width: b.width * 2.2, height: b.height * 2.2)
        let img = page.thumbnail(of: size, for: .mediaBox)
        guard let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { continue }
        let req = VNRecognizeTextRequest()
        req.recognitionLevel = .accurate
        req.recognitionLanguages = ["zh-Hans", "en-US"]
        req.usesLanguageCorrection = true
        try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
        for o in (req.results ?? []) { if let c = o.topCandidates(1).first { print(c.string) } }
    }
}
