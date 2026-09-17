//go:build !nowebview && darwin && cgo

// The menu bar, which on macOS is where the keyboard shortcuts live.
//
// A Mac application without a menu bar has no Edit menu, and without an Edit menu ⌘C, ⌘V, ⌘X, ⌘A
// and ⌘Z do nothing at all — the key equivalent is what sends `copy:`, `paste:` and the rest down
// the responder chain, and with no menu item carrying it the keystroke is swallowed before the web
// view ever sees it. It reads as the window intercepting the shortcuts. Nothing is intercepting
// them: nothing is dispatching them.
//
// So the launcher installs the ordinary menu every Mac application has — the application menu, Edit
// and Window — with the standard selectors and nothing of its own in it. WKWebView implements all
// of them already, so the items need no target: first responder is the web view, and the web view
// does the work. Windows needs none of this; WebView2 dispatches the clipboard accelerators itself
// (see menu_other.go).
package main

/*
#cgo CFLAGS: -x objective-c
#cgo LDFLAGS: -framework Cocoa
#include <stdlib.h>
#import <Cocoa/Cocoa.h>

// item adds one ordinary menu item. A mask of zero keeps AppKit's default, which is ⌘ — spelling
// that out on every line would only make the exceptions harder to see.
static void daedalus_item(NSMenu *menu, NSString *title, SEL action, NSString *key, NSUInteger mask) {
  NSMenuItem *entry = [menu addItemWithTitle:title action:action keyEquivalent:key];
  if (mask != 0) {
    [entry setKeyEquivalentModifierMask:mask];
  }
}

static NSMenu *daedalus_submenu(NSMenu *bar, NSString *title) {
  NSMenuItem *holder = [bar addItemWithTitle:title action:NULL keyEquivalent:@""];
  NSMenu *menu = [[NSMenu alloc] initWithTitle:title];
  [holder setSubmenu:menu];
  return menu;
}

// daedalus_install_menu builds the bar and hands it to the running application. It has to be called
// on the main thread, after the application object exists — which is what creating the window does.
static void daedalus_install_menu(const char *cname) {
  @autoreleasepool {
    NSApplication *app = [NSApplication sharedApplication];
    NSString *name = [NSString stringWithUTF8String:cname];
    NSMenu *bar = [[NSMenu alloc] init];

    // The application menu. Its title is not drawn — macOS uses the application's own name — but
    // the item has to be there for the ones under it to appear.
    NSMenu *appMenu = daedalus_submenu(bar, name);
    daedalus_item(appMenu, [@"About " stringByAppendingString:name], @selector(orderFrontStandardAboutPanel:), @"", 0);
    [appMenu addItem:[NSMenuItem separatorItem]];
    daedalus_item(appMenu, [@"Hide " stringByAppendingString:name], @selector(hide:), @"h", 0);
    daedalus_item(appMenu, @"Hide Others", @selector(hideOtherApplications:), @"h", NSEventModifierFlagCommand | NSEventModifierFlagOption);
    daedalus_item(appMenu, @"Show All", @selector(unhideAllApplications:), @"", 0);
    [appMenu addItem:[NSMenuItem separatorItem]];
    daedalus_item(appMenu, [@"Quit " stringByAppendingString:name], @selector(terminate:), @"q", 0);

    // Edit: the reason this file exists. Every selector here is one WKWebView already answers.
    NSMenu *editMenu = daedalus_submenu(bar, @"Edit");
    daedalus_item(editMenu, @"Undo", @selector(undo:), @"z", 0);
    daedalus_item(editMenu, @"Redo", @selector(redo:), @"z", NSEventModifierFlagCommand | NSEventModifierFlagShift);
    [editMenu addItem:[NSMenuItem separatorItem]];
    daedalus_item(editMenu, @"Cut", @selector(cut:), @"x", 0);
    daedalus_item(editMenu, @"Copy", @selector(copy:), @"c", 0);
    daedalus_item(editMenu, @"Paste", @selector(paste:), @"v", 0);
    daedalus_item(editMenu, @"Paste and Match Style", @selector(pasteAsPlainText:), @"v", NSEventModifierFlagCommand | NSEventModifierFlagOption | NSEventModifierFlagShift);
    daedalus_item(editMenu, @"Delete", @selector(delete:), @"", 0);
    daedalus_item(editMenu, @"Select All", @selector(selectAll:), @"a", 0);

    NSMenu *windowMenu = daedalus_submenu(bar, @"Window");
    daedalus_item(windowMenu, @"Minimize", @selector(performMiniaturize:), @"m", 0);
    daedalus_item(windowMenu, @"Zoom", @selector(performZoom:), @"", 0);
    [windowMenu addItem:[NSMenuItem separatorItem]];
    daedalus_item(windowMenu, @"Close", @selector(performClose:), @"w", 0);
    // Telling the application which menu is the window menu is what keeps the list of open windows
    // in it up to date.
    [app setWindowsMenu:windowMenu];

    [app setMainMenu:bar];
  }
}
*/
import "C"

import "unsafe"

// installMenu puts the standard menu bar in place. It is called from openWindow, on the goroutine
// main runs on, once the web view has created the application object.
func installMenu(name string) {
	cname := C.CString(name)
	defer C.free(unsafe.Pointer(cname))
	C.daedalus_install_menu(cname)
}
