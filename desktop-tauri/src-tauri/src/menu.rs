//! Native application menu — 文件、编辑、视图、窗口、帮助.

use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem, Submenu},
    AppHandle, Emitter, Manager,
};

pub fn create_menu(app: &AppHandle) -> tauri::Result<Menu<tauri::Wry>> {
    // 文件菜单
    let new_chat = MenuItem::with_id(app, "menu_new_chat", "新对话", true, Some("CmdOrCtrl+N"))?;
    let settings = MenuItem::with_id(app, "menu_settings", "设置", true, Some("CmdOrCtrl+,"))?;
    // Use an application-owned quit item. The native predefined quit action
    // can terminate Cocoa directly before Tauri's async backend cleanup has
    // completed, leaving the Python process orphaned.
    let quit = MenuItem::with_id(app, "menu_quit", "退出", true, Some("CmdOrCtrl+Q"))?;
    let file_menu = Submenu::with_items(
        app,
        "文件",
        true,
        &[
            &new_chat,
            &PredefinedMenuItem::separator(app)?,
            &settings,
            &PredefinedMenuItem::separator(app)?,
            &quit,
        ],
    )?;

    // 编辑菜单
    let edit_menu = Submenu::with_items(
        app,
        "编辑",
        true,
        &[
            &PredefinedMenuItem::undo(app, Some("撤销"))?,
            &PredefinedMenuItem::redo(app, Some("重做"))?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::cut(app, Some("剪切"))?,
            &PredefinedMenuItem::copy(app, Some("复制"))?,
            &PredefinedMenuItem::paste(app, Some("粘贴"))?,
            &PredefinedMenuItem::select_all(app, Some("全选"))?,
        ],
    )?;

    // 视图菜单
    let toggle_sidebar = MenuItem::with_id(
        app,
        "menu_toggle_sidebar",
        "切换侧边栏",
        true,
        Some("CmdOrCtrl+Shift+S"),
    )?;
    let reload = MenuItem::with_id(app, "menu_reload", "重新加载", true, Some("CmdOrCtrl+R"))?;
    let dev_tools = MenuItem::with_id(
        app,
        "menu_dev_tools",
        "开发者工具",
        true,
        Some("CmdOrCtrl+Shift+I"),
    )?;
    let view_menu = Submenu::with_items(
        app,
        "视图",
        true,
        &[
            &toggle_sidebar,
            &PredefinedMenuItem::separator(app)?,
            &reload,
            &dev_tools,
        ],
    )?;

    // 窗口菜单
    let minimize = PredefinedMenuItem::minimize(app, Some("最小化"))?;
    let zoom = PredefinedMenuItem::maximize(app, Some("缩放"))?;
    let fullscreen = PredefinedMenuItem::fullscreen(app, Some("进入全屏"))?;
    let window_menu = Submenu::with_items(
        app,
        "窗口",
        true,
        &[
            &minimize,
            &zoom,
            &PredefinedMenuItem::separator(app)?,
            &fullscreen,
        ],
    )?;

    // 帮助菜单
    let about = PredefinedMenuItem::about(app, Some("关于苏小有"), None)?;
    let help_menu = Submenu::with_items(app, "帮助", true, &[&about])?;

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
