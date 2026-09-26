package view

import (
	"bytes"
	"image"
	"image/jpeg"
)

// Scale decodes a JPEG and encodes it again to fit maxW×maxH at quality, keeping its aspect ratio.
// A picture already small enough is only re-encoded when the quality asks for it. The filter is a box
// average over the source pixels each target pixel covers, on the YCbCr planes Chromium's JPEGs come
// in, which costs a few milliseconds for a 1280×800 frame; a thumbnail is made once a second at most.
func Scale(src []byte, maxW, maxH, quality int) ([]byte, int, int, error) {
	img, err := jpeg.Decode(bytes.NewReader(src))
	if err != nil {
		return nil, 0, 0, err
	}
	b := img.Bounds()
	w, h := fit(b.Dx(), b.Dy(), maxW, maxH)
	var out image.Image
	if ycc, ok := img.(*image.YCbCr); ok {
		out = scaleYCbCr(ycc, w, h)
	} else {
		out = scaleAny(img, w, h)
	}
	var buf bytes.Buffer
	if err := jpeg.Encode(&buf, out, &jpeg.Options{Quality: quality}); err != nil {
		return nil, 0, 0, err
	}
	return buf.Bytes(), w, h, nil
}

// fit is the largest size within maxW×maxH with the aspect ratio of w×h, never larger than w×h.
func fit(w, h, maxW, maxH int) (int, int) {
	if w <= maxW && h <= maxH {
		return w, h
	}
	if w*maxH > h*maxW {
		return maxW, max(1, h*maxW/w)
	}
	return max(1, w*maxH/h), maxH
}

func scaleYCbCr(src *image.YCbCr, w, h int) *image.YCbCr {
	dst := image.NewYCbCr(image.Rect(0, 0, w, h), image.YCbCrSubsampleRatio444)
	b := src.Bounds()
	sw, sh := b.Dx(), b.Dy()
	for y := 0; y < h; y++ {
		y0, y1 := y*sh/h, max((y+1)*sh/h, y*sh/h+1)
		for x := 0; x < w; x++ {
			x0, x1 := x*sw/w, max((x+1)*sw/w, x*sw/w+1)
			var sy, scb, scr, n uint32
			for yy := y0; yy < y1; yy++ {
				for xx := x0; xx < x1; xx++ {
					px, py := b.Min.X+xx, b.Min.Y+yy
					sy += uint32(src.Y[src.YOffset(px, py)])
					ci := src.COffset(px, py)
					scb += uint32(src.Cb[ci])
					scr += uint32(src.Cr[ci])
					n++
				}
			}
			i := dst.YOffset(x, y)
			dst.Y[i] = uint8(sy / n)
			ci := dst.COffset(x, y)
			dst.Cb[ci] = uint8(scb / n)
			dst.Cr[ci] = uint8(scr / n)
		}
	}
	return dst
}

func scaleAny(src image.Image, w, h int) *image.RGBA {
	dst := image.NewRGBA(image.Rect(0, 0, w, h))
	b := src.Bounds()
	sw, sh := b.Dx(), b.Dy()
	for y := 0; y < h; y++ {
		y0, y1 := y*sh/h, max((y+1)*sh/h, y*sh/h+1)
		for x := 0; x < w; x++ {
			x0, x1 := x*sw/w, max((x+1)*sw/w, x*sw/w+1)
			var r, g, bl, n uint32
			for yy := y0; yy < y1; yy++ {
				for xx := x0; xx < x1; xx++ {
					cr, cg, cb, _ := src.At(b.Min.X+xx, b.Min.Y+yy).RGBA()
					r += cr >> 8
					g += cg >> 8
					bl += cb >> 8
					n++
				}
			}
			i := dst.PixOffset(x, y)
			dst.Pix[i], dst.Pix[i+1], dst.Pix[i+2], dst.Pix[i+3] = uint8(r/n), uint8(g/n), uint8(bl/n), 255
		}
	}
	return dst
}
