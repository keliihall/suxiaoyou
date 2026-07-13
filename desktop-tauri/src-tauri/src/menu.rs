//! Native application menu with runtime Chinese/English localization.

use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem, Submenu},
    AppHandle, Emitter, Manager,
};

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum UiLanguage {
    Zh,
    #[default]
    En,
}

impl UiLanguage {
    pub fn parse(value: &str) -> Result<Self, String> {
        match value.trim().to_ascii_lowercase().as_str() {
            "zh" | "zh-cn" | "zh_cn" => Ok(Self::Zh),
            "en" | "en-us" | "en_us" => Ok(Self::En),
            _ => Err("Unsupported UI language; expected 'en' or 'zh'".to_string()),
        }
    }

    pub fn from_locale(value: &str) -> Self {
        if value.trim().to_ascii_lowercase().starts_with("zh") {
            Self::Zh
        } else {
            Self::En
        }
    }

    pub fn detect_system() -> Self {
        system_locale()
            .or_else(|| {
                ["LC_ALL", "LC_MESSAGES", "LANG"].iter().find_map(|key| {
                    std::env::var(key)
                        .ok()
                        .filter(|value| !value.trim().is_empty())
                })
            })
            .map_or(Self::En, |locale| Self::from_locale(&locale))
    }

    pub fn app_name(self) -> &'static str {
        match self {
            Self::Zh => "苏小有",
            Self::En => "suyo",
        }
    }
}

#[cfg(target_os = "macos")]
fn system_locale() -> Option<String> {
    let languages = std::process::Command::new("defaults")
        .args(["read", "-g", "AppleLanguages"])
        .output()
        .ok()
        .filter(|output| output.status.success())
        .and_then(|output| String::from_utf8(output.stdout).ok());
    if let Some(language) = languages.as_deref().and_then(|output| {
        output.lines().find_map(|line| {
            let value = line.trim().trim_end_matches(',').trim_matches('"');
            (!value.is_empty() && value != "(" && value != ")").then(|| value.to_string())
        })
    }) {
        return Some(language);
    }

    std::process::Command::new("defaults")
        .args(["read", "-g", "AppleLocale"])
        .output()
        .ok()
        .filter(|output| output.status.success())
        .and_then(|output| String::from_utf8(output.stdout).ok())
        .map(|locale| locale.trim().to_string())
        .filter(|locale| !locale.is_empty())
}

#[cfg(target_os = "windows")]
fn system_locale() -> Option<String> {
    use windows_sys::Win32::Globalization::{GetUserDefaultUILanguage, LCIDToLocaleName};

    // Use the Windows display-language preference, not the separate regional
    // formatting locale. The frontend synchronizes its persisted choice after
    // startup, while this selects the correct native menu for the first frame.
    let mut locale = [0_u16; 85];
    let language_id = unsafe { GetUserDefaultUILanguage() };
    let length = unsafe {
        LCIDToLocaleName(
            u32::from(language_id),
            locale.as_mut_ptr(),
            locale.len() as i32,
            0,
        )
    };
    (length > 1).then(|| String::from_utf16_lossy(&locale[..length as usize - 1]))
}

#[cfg(not(any(target_os = "macos", target_os = "windows")))]
fn system_locale() -> Option<String> {
    None
}

#[derive(Debug, Clone, Copy)]
struct MenuLabels {
    file: &'static str,
    new_chat: &'static str,
    settings: &'static str,
    quit: &'static str,
    edit: &'static str,
    undo: &'static str,
    redo: &'static str,
    cut: &'static str,
    copy: &'static str,
    paste: &'static str,
    select_all: &'static str,
    view: &'static str,
    toggle_sidebar: &'static str,
    reload: &'static str,
    developer_tools: &'static str,
    window: &'static str,
    minimize: &'static str,
    zoom: &'static str,
    fullscreen: &'static str,
    help: &'static str,
    about: &'static str,
}

impl MenuLabels {
    fn for_language(language: UiLanguage) -> Self {
        match language {
            UiLanguage::Zh => Self {
                file: "文件",
                new_chat: "新对话",
                settings: "设置",
                quit: "退出",
                edit: "编辑",
                undo: "撤销",
                redo: "重做",
                cut: "剪切",
                copy: "复制",
                paste: "粘贴",
                select_all: "全选",
                view: "视图",
                toggle_sidebar: "切换侧边栏",
                reload: "重新加载",
                developer_tools: "开发者工具",
                window: "窗口",
                minimize: "最小化",
                zoom: "缩放",
                fullscreen: "进入全屏",
                help: "帮助",
                about: "关于苏小有",
            },
            UiLanguage::En => Self {
                file: "File",
                new_chat: "New Chat",
                settings: "Settings",
                quit: "Quit",
                edit: "Edit",
                undo: "Undo",
                redo: "Redo",
                cut: "Cut",
                copy: "Copy",
                paste: "Paste",
                select_all: "Select All",
                view: "View",
                toggle_sidebar: "Toggle Sidebar",
                reload: "Reload",
                developer_tools: "Developer Tools",
                window: "Window",
                minimize: "Minimize",
                zoom: "Zoom",
                fullscreen: "Enter Full Screen",
                help: "Help",
                about: "About suyo",
            },
        }
    }
}

pub fn create_menu(app: &AppHandle, language: UiLanguage) -> tauri::Result<Menu<tauri::Wry>> {
    let labels = MenuLabels::for_language(language);
    let new_chat = MenuItem::with_id(
        app,
        "menu_new_chat",
        labels.new_chat,
        true,
        Some("CmdOrCtrl+N"),
    )?;
    let settings = MenuItem::with_id(
        app,
        "menu_settings",
        labels.settings,
        true,
        Some("CmdOrCtrl+,"),
    )?;
    // Use an application-owned quit item. The native predefined quit action
    // can terminate Cocoa directly before Tauri's async backend cleanup has
    // completed, leaving the Python process orphaned.
    let quit = MenuItem::with_id(app, "menu_quit", labels.quit, true, Some("CmdOrCtrl+Q"))?;
    let file_menu = Submenu::with_items(
        app,
        labels.file,
        true,
        &[
            &new_chat,
            &PredefinedMenuItem::separator(app)?,
            &settings,
            &PredefinedMenuItem::separator(app)?,
            &quit,
        ],
    )?;

    let edit_menu = Submenu::with_items(
        app,
        labels.edit,
        true,
        &[
            &PredefinedMenuItem::undo(app, Some(labels.undo))?,
            &PredefinedMenuItem::redo(app, Some(labels.redo))?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::cut(app, Some(labels.cut))?,
            &PredefinedMenuItem::copy(app, Some(labels.copy))?,
            &PredefinedMenuItem::paste(app, Some(labels.paste))?,
            &PredefinedMenuItem::select_all(app, Some(labels.select_all))?,
        ],
    )?;

    let toggle_sidebar = MenuItem::with_id(
        app,
        "menu_toggle_sidebar",
        labels.toggle_sidebar,
        true,
        Some("CmdOrCtrl+Shift+S"),
    )?;
    let reload = MenuItem::with_id(app, "menu_reload", labels.reload, true, Some("CmdOrCtrl+R"))?;
    let dev_tools = MenuItem::with_id(
        app,
        "menu_dev_tools",
        labels.developer_tools,
        true,
        Some("CmdOrCtrl+Shift+I"),
    )?;
    let view_menu = Submenu::with_items(
        app,
        labels.view,
        true,
        &[
            &toggle_sidebar,
            &PredefinedMenuItem::separator(app)?,
            &reload,
            &dev_tools,
        ],
    )?;

    let minimize = PredefinedMenuItem::minimize(app, Some(labels.minimize))?;
    let zoom = PredefinedMenuItem::maximize(app, Some(labels.zoom))?;
    let fullscreen = PredefinedMenuItem::fullscreen(app, Some(labels.fullscreen))?;
    let window_menu = Submenu::with_items(
        app,
        labels.window,
        true,
        &[
            &minimize,
            &zoom,
            &PredefinedMenuItem::separator(app)?,
            &fullscreen,
        ],
    )?;

    let about = PredefinedMenuItem::about(app, Some(labels.about), None)?;
    let help_menu = Submenu::with_items(app, labels.help, true, &[&about])?;

    let menu = Menu::with_items(
        app,
        &[&file_menu, &edit_menu, &view_menu, &window_menu, &help_menu],
    )?;

    Ok(menu)
}

/// Handle menu events.
pub fn handle_menu_event(app: &AppHandle, event_id: &str) {
    if event_id == "menu_quit" {
        app.exit(0);
        return;
    }

    let Some(window) = app.get_webview_window("main") else {
        return;
    };

    match event_id {
        "menu_new_chat" => {
            let _ = window.emit("navigate", "/c/new");
        }
        "menu_settings" => {
            let _ = window.emit("navigate", "/settings");
        }
        "menu_toggle_sidebar" => {
            let _ = window.emit("toggle-sidebar", ());
        }
        "menu_reload" => {
            let _ = window.eval("window.location.reload()");
        }
        "menu_dev_tools" => {
            if window.is_devtools_open() {
                window.close_devtools();
            } else {
                window.open_devtools();
            }
        }
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use super::{MenuLabels, UiLanguage};

    #[test]
    fn accepts_supported_ui_language_variants() {
        assert_eq!(UiLanguage::parse("zh-CN").unwrap(), UiLanguage::Zh);
        assert_eq!(UiLanguage::parse("en_US").unwrap(), UiLanguage::En);
        assert!(UiLanguage::parse("fr").is_err());
    }

    #[test]
    fn maps_brand_and_native_labels_by_language() {
        assert_eq!(UiLanguage::Zh.app_name(), "苏小有");
        assert_eq!(UiLanguage::En.app_name(), "suyo");
        assert_eq!(MenuLabels::for_language(UiLanguage::Zh).file, "文件");
        assert_eq!(MenuLabels::for_language(UiLanguage::En).file, "File");
        assert_eq!(MenuLabels::for_language(UiLanguage::En).about, "About suyo");
    }

    #[test]
    fn detects_chinese_locale_family() {
        assert_eq!(UiLanguage::from_locale("zh_CN.UTF-8"), UiLanguage::Zh);
        assert_eq!(UiLanguage::from_locale("en_US.UTF-8"), UiLanguage::En);
    }
}
