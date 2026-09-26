package view

import "github.com/ascorblack/daedalus/browserd/internal/browser"

// testTab is a tab with an id and nothing behind it: what a client's frame names.
func testTab() *browser.Tab { return &browser.Tab{ID: "t1"} }
