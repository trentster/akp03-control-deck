// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "AKP03MenuBar",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "AKP03MenuBarCore", targets: ["AKP03MenuBarCore"]),
        .library(name: "AKP03ControlServiceCore", targets: ["AKP03ControlServiceCore"]),
        .library(name: "AKP03HIDStreamCore", targets: ["AKP03HIDStreamCore"]),
        .library(name: "AKP03IOKitInput", targets: ["AKP03IOKitInput"]),
        .library(name: "AKP03KeyInjectCore", targets: ["AKP03KeyInjectCore"]),
        .executable(name: "AKP03MenuBar", targets: ["AKP03MenuBar"]),
        .executable(name: "AKP03HIDReadBridge", targets: ["AKP03HIDReadBridge"]),
        .executable(name: "AKP03HIDStream", targets: ["AKP03HIDStream"]),
        .executable(name: "AKP03ControlService", targets: ["AKP03ControlService"]),
        .executable(name: "AKP03KeyInject", targets: ["AKP03KeyInject"]),
    ],
    targets: [
        .target(
            name: "AKP03MenuBarCore",
            dependencies: ["AKP03ControlServiceCore"]
        ),
        .target(name: "AKP03ControlServiceCore"),
        .target(name: "AKP03KeyInjectCore"),
        .target(
            name: "AKP03HIDStreamCore",
            dependencies: ["AKP03ControlServiceCore"]
        ),
        .target(
            name: "AKP03IOKitInput",
            dependencies: ["AKP03ControlServiceCore"],
            linkerSettings: [.linkedFramework("IOKit")]
        ),
        .executableTarget(
            name: "AKP03ControlService",
            dependencies: ["AKP03ControlServiceCore"]
        ),
        .executableTarget(
            name: "AKP03KeyInject",
            dependencies: ["AKP03KeyInjectCore"],
            linkerSettings: [.linkedFramework("ApplicationServices")]
        ),
        .executableTarget(
            name: "AKP03MenuBar",
            dependencies: ["AKP03MenuBarCore"],
            linkerSettings: [.linkedFramework("IOKit"), .linkedFramework("ApplicationServices")]
        ),
        .executableTarget(
            name: "AKP03HIDReadBridge",
            dependencies: ["AKP03MenuBarCore", "AKP03IOKitInput"]
        ),
        .executableTarget(
            name: "AKP03HIDStream",
            dependencies: ["AKP03ControlServiceCore", "AKP03HIDStreamCore", "AKP03IOKitInput"],
            linkerSettings: [.linkedFramework("IOKit")]
        ),
        .executableTarget(
            name: "AKP03MenuBarChecks",
            dependencies: ["AKP03MenuBarCore", "AKP03ControlServiceCore", "AKP03HIDStreamCore", "AKP03IOKitInput", "AKP03KeyInjectCore"],
            path: "Checks"
        ),
        .testTarget(
            name: "AKP03ControlServiceCoreTests",
            dependencies: ["AKP03ControlServiceCore", "AKP03IOKitInput"]
        ),
    ]
)
