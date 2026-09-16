package main

import (
	"encoding/json"
	"os"
	"path/filepath"
)

// Geometry is where the window was when it was last closed. It lives in the data folder beside
// everything else the installation owns, so moving the folder moves it too and deleting the folder
// forgets it.
type Geometry struct {
	Width  int `json:"width"`
	Height int `json:"height"`
	X      int `json:"x"`
	Y      int `json:"y"`
}

// The default is a window wide enough for the app's two-column layout and short enough to open
// whole on a laptop screen.
const (
	defaultWidth  = 1180
	defaultHeight = 820
	minWidth      = 480
	minHeight     = 400
	maxDimension  = 8000
)

// geometryFile is where the numbers are kept.
func geometryFile(p Paths) string { return filepath.Join(p.Data, "window.json") }

// DefaultGeometry is the window a machine that has never opened one gets.
func DefaultGeometry() Geometry {
	return Geometry{Width: defaultWidth, Height: defaultHeight}
}

// ReadGeometry takes what the last run wrote, and the default whenever that file is missing,
// unreadable or nonsense. A window restored to a size no one can use, or onto a screen that is no
// longer attached, is worse than a window the desktop places itself.
func ReadGeometry(p Paths) Geometry {
	data, err := os.ReadFile(geometryFile(p))
	if err != nil {
		return DefaultGeometry()
	}
	var stored Geometry
	if err := json.Unmarshal(data, &stored); err != nil {
		return DefaultGeometry()
	}
	return DefaultGeometry().With(stored.Width, stored.Height, stored.X, stored.Y)
}

// With is the geometry this one becomes when the window reports those numbers: a size that is
// usable, or the one already held, and a position that is on a screen rather than off it.
func (g Geometry) With(width, height, x, y int) Geometry {
	out := g
	if width >= minWidth && width <= maxDimension {
		out.Width = width
	}
	if height >= minHeight && height <= maxDimension {
		out.Height = height
	}
	if x >= 0 && y >= 0 && x <= maxDimension && y <= maxDimension {
		out.X, out.Y = x, y
	}
	return out
}

// WriteGeometry records the window for the next start. A failure is nothing to report: the next
// start opens the default window, which is where every first start opens.
func WriteGeometry(p Paths, g Geometry) {
	data, err := json.Marshal(g)
	if err != nil {
		return
	}
	_ = os.WriteFile(geometryFile(p), data, 0o600)
}
