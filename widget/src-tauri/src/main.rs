// Relay widget - the mascot, floating outside the terminal.
//
// Read-only by design: it renders what relay publishes to ~/.relay/widget.json
// and derives nothing. See docs/specs/2026-07-28-desktop-widget-design.md.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::Manager;

/// Make the window survive another app going fullscreen.
///
/// `alwaysOnTop` + `visibleOnAllWorkspaces` is not enough on its own: tao's
/// `set_visible_on_all_workspaces` sets only `CanJoinAllSpaces`
/// (tao-0.34.5/src/platform_impl/macos/window.rs:1534), which follows you
/// between normal Spaces but does not put the window over another app's
/// fullscreen Space. `FullScreenAuxiliary` is the bit that does, and nothing in
/// Tauri's API surface exposes it - hence going straight at NSWindow.
///
/// This matters more than it sounds: a fullscreen browser or editor is exactly
/// when you are away from the terminal and most want to see the creature. A
/// widget that vanishes precisely then is a widget with no purpose.
#[cfg(target_os = "macos")]
fn float_above_fullscreen(window: &tauri::WebviewWindow) {
    use objc2_app_kit::{NSWindow, NSWindowCollectionBehavior, NSWindowStyleMask};

    let Ok(ptr) = window.ns_window() else { return };
    if ptr.is_null() {
        return;
    }
    // Safety: Tauri hands back a live NSWindow for this webview, and setup runs
    // on the main thread, which is where AppKit requires this call.
    let ns: &NSWindow = unsafe { &*(ptr as *const NSWindow) };
    ns.setCollectionBehavior(
        NSWindowCollectionBehavior::CanJoinAllSpaces
            | NSWindowCollectionBehavior::FullScreenAuxiliary
            | NSWindowCollectionBehavior::Stationary,
    );
    // Never steal the keyboard. An always-on-top window that can become key
    // takes your keystrokes the moment it appears - which made typing anywhere
    // else miserable while it was launching. It has no text input and nothing
    // to type into; clicks still work, focus does not move.
    ns.setStyleMask(ns.styleMask() | NSWindowStyleMask::NonactivatingPanel);
}

#[cfg(not(target_os = "macos"))]
fn float_above_fullscreen(_window: &tauri::WebviewWindow) {}

/// Raise iTerm2, and the specific session if we were given one.
///
/// Navigation, not control: this cannot arm, approve, pause or inject anything,
/// so it does not breach the widget's read-only contract. It is the other half
/// of an alarm - the creature says "2 need you" and this is how you get there.
///
/// The AppleScript mirrors iterm/focus_session.sh (which relay's notifications
/// already use). It is inlined rather than shelling out to that script so the
/// widget stays self-contained and never executes a path handed to it by a file
/// on disk. `sid` is validated as an iTerm2 GUID for the same reason: nothing
/// from widget.json reaches a shell uninspected.
#[tauri::command]
fn focus_iterm(sid: Option<String>) {
    let sid = sid.filter(|s| {
        s.len() == 36 && s.bytes().all(|b| b.is_ascii_hexdigit() || b == b'-')
    });
    let script = match sid {
        Some(s) => format!(
            r#"tell application "iTerm2"
                 repeat with w in windows
                   repeat with t in tabs of w
                     repeat with ss in sessions of t
                       if id of ss is "{s}" then
                         select t
                         select ss
                         set index of w to 1
                         activate
                         return
                       end if
                     end repeat
                   end repeat
                 end repeat
                 activate
               end tell"#
        ),
        None => r#"tell application "iTerm2" to activate"#.to_string(),
    };
    // Best-effort and silent, exactly like focus_session.sh: a closed tab or an
    // AppleScript hiccup must never surface an error in an ambient widget.
    let _ = std::process::Command::new("osascript")
        .arg("-e")
        .arg(script)
        .output();
}

fn main() {
    tauri::Builder::default()
        // Singleton, for the same reason relay's TUI takes a lock: a second
        // copy is never what anyone wants, and orphans accumulate silently.
        // relay only tracks the child IT spawned, so a relay killed without a
        // clean quit leaves a widget behind that the next launch knows nothing
        // about. Here a second process raises the existing window and exits.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            use tauri::Manager;
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_fs::init())
        .invoke_handler(tauri::generate_handler![focus_iterm])
        .setup(|app| {
            // Accessory, not a regular app: no Dock icon, never becomes the
            // frontmost application, never takes focus on launch. An ambient
            // read-only display should be furniture, not something you have to
            // click out of - and it is why a pile of these was so intrusive.
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);
            if let Some(w) = app.get_webview_window("main") {
                float_above_fullscreen(&w);
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("relay widget failed to start");
}
