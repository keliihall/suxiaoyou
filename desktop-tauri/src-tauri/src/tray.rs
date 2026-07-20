//! System tray — Codex-style menu with dynamic recent chats.
//!
//! The menu is rebuilt whenever the frontend pushes a new list of recents
//! via the `update_tray_recents` command. Item IDs of the form `recent:<id>`
//! route to the corresponding chat; everything else is a well-known static ID.

use serde::Deserialize;
use tauri::{
    image::Image,
    menu::{Menu, MenuItem, PredefinedMenuItem, Submenu},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Emitter, Manager,
};

use crate::{menu::UiLanguage, window_lifecycle};

const TRAY_ID: &str = "main-tray";
const RECENT_PREFIX: &str = "recent:";
const MAX_TITLE_CHARS: usize = 48;

#[derive(Debug, Clone, Deserialize)]
pub struct TrayRecent {
    pub id: String,
    pub title: Option<String>,
}

pub fn create_tray(app: &AppHandle, language: UiLanguage) -> tauri::Result<()> {
    #[cfg(target_os = "macos")]
    let tray_icon = Image::from_bytes(include_bytes!("../icons/tray-template@2x.png"))?;
    #[cfg(not(target_os = "macos"))]
    let tray_icon = Image::from_bytes(include_bytes!("../icons/512x512.png"))?;

    let menu = build_menu(app, &[], language)?;

    let builder = TrayIconBuilder::with_id(TRAY_ID)
        .icon(tray_icon)
        .tooltip(language.app_name())
        .menu(&menu);

    #[cfg(target_os = "macos")]
    let builder = builder.icon_as_template(true);

    builder
        .on_menu_event(|app, event| handle_menu_event(app, event.id().as_ref()))
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                if let Some(window) = tray.app_handle().get_webview_window("main") {
                    window_lifecycle::show_and_focus(&window);
                }
            }
        })
        .build(app)?;

    Ok(())
}

/// Rebuild the tray menu with the given recent-chats list.
pub fn set_tray(
    app: &AppHandle,
    recents: &[TrayRecent],
    language: UiLanguage,
) -> tauri::Result<()> {
    let menu = build_menu(app, recents, language)?;
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        tray.set_menu(Some(menu))?;
        tray.set_tooltip(Some(language.app_name()))?;
    }
    Ok(())
}

fn build_menu(
    app: &AppHandle,
    recents: &[TrayRecent],
    language: UiLanguage,
) -> tauri::Result<Menu<tauri::Wry>> {
    let labels = TrayLabels::for_language(language);
    let new_chat = MenuItem::with_id(app, "new_chat", labels.new_chat, true, None::<&str>)?;
    let search_chats =
        MenuItem::with_id(app, "search_chats", labels.search_chats, true, None::<&str>)?;

    let recent_submenu = build_recent_submenu(app, recents, labels)?;

    let show_window =
        MenuItem::with_id(app, "show_window", labels.show_window, true, None::<&str>)?;
    let settings = MenuItem::with_id(app, "settings", labels.settings, true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", labels.quit, true, None::<&str>)?;

    Menu::with_items(
        app,
        &[
            &new_chat,
            &search_chats,
            &PredefinedMenuItem::separator(app)?,
            &recent_submenu,
            &PredefinedMenuItem::separator(app)?,
            &show_window,
            &settings,
            &PredefinedMenuItem::separator(app)?,
            &quit,
        ],
    )
}

fn build_recent_submenu(
    app: &AppHandle,
    recents: &[TrayRecent],
    labels: TrayLabels,
) -> tauri::Result<Submenu<tauri::Wry>> {
    if recents.is_empty() {
        let empty = MenuItem::with_id(
            app,
            "recent_empty",
            labels.recent_empty,
            false,
            None::<&str>,
        )?;
        return Submenu::with_items(app, labels.recent_chats, true, &[&empty]);
    }

    let mut items: Vec<Box<dyn tauri::menu::IsMenuItem<tauri::Wry>>> = Vec::new();
    for r in recents {
        let label = format_title(r.title.as_deref(), labels.untitled_chat);
        let id = format!("{RECENT_PREFIX}{}", r.id);
        items.push(Box::new(MenuItem::with_id(
            app,
            id,
            label,
            true,
            None::<&str>,
        )?));
    }
    items.push(Box::new(PredefinedMenuItem::separator(app)?));
    items.push(Box::new(MenuItem::with_id(
        app,
        "recent_show_all",
        labels.show_all_chats,
        true,
        None::<&str>,
    )?));

    let refs: Vec<&dyn tauri::menu::IsMenuItem<tauri::Wry>> =
        items.iter().map(|b| b.as_ref()).collect();
    Submenu::with_items(app, labels.recent_chats, true, &refs)
}

fn format_title(raw: Option<&str>, untitled: &str) -> String {
    let title = raw
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or(untitled);
    if title.chars().count() <= MAX_TITLE_CHARS {
        return title.to_string();
    }
    let truncated: String = title.chars().take(MAX_TITLE_CHARS).collect();
    format!("{truncated}…")
}

#[derive(Debug, Clone, Copy)]
struct TrayLabels {
    new_chat: &'static str,
    search_chats: &'static str,
    recent_chats: &'static str,
    recent_empty: &'static str,
    show_all_chats: &'static str,
    untitled_chat: &'static str,
    show_window: &'static str,
    settings: &'static str,
    quit: &'static str,
}

impl TrayLabels {
    fn for_language(language: UiLanguage) -> Self {
        match language {
            UiLanguage::Zh => Self {
                new_chat: "新对话",
                search_chats: "搜索对话…",
                recent_chats: "最近对话",
                recent_empty: "暂无最近对话",
                show_all_chats: "显示全部对话",
                untitled_chat: "未命名对话",
                show_window: "打开苏小有",
                settings: "设置",
                quit: "退出苏小有",
            },
            UiLanguage::En => Self {
                new_chat: "New Chat",
                search_chats: "Search Chats…",
                recent_chats: "Recent Chats",
                recent_empty: "No Recent Chats",
                show_all_chats: "Show All Chats",
                untitled_chat: "Untitled Chat",
                show_window: "Open suyo",
                settings: "Settings",
                quit: "Quit suyo",
            },
        }
    }
}

fn recent_chat_route(session_id: &str) -> String {
    let encoded: String = url::form_urlencoded::byte_serialize(session_id.as_bytes()).collect();
    format!("/c/_?sessionId={encoded}")
}

fn handle_menu_event(app: &AppHandle, event_id: &str) {
    let Some(window) = app.get_webview_window("main") else {
        return;
    };

    let show_and_focus = || {
        window_lifecycle::show_and_focus(&window);
    };

    if let Some(session_id) = event_id.strip_prefix(RECENT_PREFIX) {
        show_and_focus();
        let _ = window.emit("navigate", recent_chat_route(session_id));
        return;
    }

    match event_id {
        "new_chat" => {
            show_and_focus();
            let _ = window.emit("navigate", "/c/new");
        }
        "search_chats" => {
            show_and_focus();
            let _ = window.emit("open-search", ());
        }
        "recent_show_all" | "show_window" => {
            show_and_focus();
        }
        "settings" => {
            show_and_focus();
            let _ = window.emit("navigate", "/settings");
        }
        "quit" => {
            app.exit(0);
        }
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use super::{format_title, recent_chat_route, TrayLabels};
    use crate::menu::UiLanguage;

    #[test]
    fn recent_chat_uses_static_export_compatible_route() {
        assert_eq!(
            recent_chat_route("session/with spaces"),
            "/c/_?sessionId=session%2Fwith+spaces"
        );
    }

    #[test]
    fn localizes_tray_brand_and_empty_title() {
        let zh = TrayLabels::for_language(UiLanguage::Zh);
        let en = TrayLabels::for_language(UiLanguage::En);
        assert_eq!(zh.show_window, "打开苏小有");
        assert_eq!(en.show_window, "Open suyo");
        assert_eq!(format_title(None, zh.untitled_chat), "未命名对话");
        assert_eq!(format_title(Some("  "), en.untitled_chat), "Untitled Chat");
    }
}
