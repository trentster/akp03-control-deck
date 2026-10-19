import AKP03ControlServiceCore
import Foundation

@main
struct AKP03ControlServiceMain {
    static func main() {
        do {
            switch try AKP03ControlServiceCommand.parse(
                arguments: Array(CommandLine.arguments.dropFirst())
            ) {
            case .help:
                print(akp03ControlServiceUsage)
            case .check:
                let service = AKP03ControlService()
                let snapshot = service.snapshot
                print(
                    "AKP03 control-service interface ready "
                        + "(status=\(snapshot.status.rawValue), "
                        + "accepted-events=\(snapshot.acceptedEventCount))."
                )
            }
        } catch {
            FileHandle.standardError.write(
                Data("AKP03 control service: \(error.localizedDescription)\n".utf8)
            )
            FileHandle.standardError.write(Data("\(akp03ControlServiceUsage)\n".utf8))
            exit(EXIT_FAILURE)
        }
    }
}
