// Relay widget - the mascot, floating outside the terminal.
//
// Read-only by design: it renders what relay publishes to ~/.relay/widget.json
// and derives nothing. See docs/specs/2026-07-28-desktop-widget-design.md.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_fs::init())
        .run(tauri::generate_context!())
        .expect("relay widget failed to start");
}
