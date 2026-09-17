import Foundation
import PDFKit
// 用法: pdftext <file.pdf...> → 输出全部文字
for path in CommandLine.arguments.dropFirst() {
    guard let doc = PDFDocument(url: URL(fileURLWithPath: path)) else { continue }
    for i in 0..<doc.pageCount {
        if let page = doc.page(at: i), let s = page.string { print(s) }
    }
}
