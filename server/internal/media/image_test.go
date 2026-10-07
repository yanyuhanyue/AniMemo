package media

import (
	"bytes"
	"encoding/binary"
	"errors"
	"hash/crc32"
	"image"
	"image/jpeg"
	"image/png"
	"testing"

	"animemo.local/server/internal/fault"
)

func TestValidateImage(t *testing.T) {
	var pngData, jpegData bytes.Buffer
	picture := image.NewRGBA(image.Rect(0, 0, 2, 3))
	if err := png.Encode(&pngData, picture); err != nil {
		t.Fatal(err)
	}
	if err := jpeg.Encode(&jpegData, picture, nil); err != nil {
		t.Fatal(err)
	}
	for kind, data := range map[string][]byte{"image/png": pngData.Bytes(), "image/jpeg": jpegData.Bytes()} {
		cover, err := Validate(data, kind)
		if err != nil || cover.Width != 2 || cover.Height != 3 || cover.ContentType != kind || !bytes.Equal(cover.Data, data) {
			t.Fatalf("valid %s: %+v, %v", kind, cover, err)
		}
	}
	largeHeader := func(width, height uint32) []byte {
		data := bytes.Clone(pngData.Bytes())
		binary.BigEndian.PutUint32(data[16:20], width)
		binary.BigEndian.PutUint32(data[20:24], height)
		binary.BigEndian.PutUint32(data[29:33], crc32.ChecksumIEEE(data[12:29]))
		return data
	}
	for _, test := range []struct {
		name, kind, code string
		data             []byte
	}{
		{"empty", "image/png", "validation_error", nil},
		{"spoof", "image/png", "validation_error", []byte("<svg></svg>")},
		{"mismatch", "image/jpeg", "validation_error", pngData.Bytes()},
		{"svg", "image/svg+xml", "unsupported_media_type", []byte("<svg></svg>")},
		{"missing type", "", "unsupported_media_type", pngData.Bytes()},
		{"truncated", "image/png", "validation_error", pngData.Bytes()[:40]},
		{"bytes", "image/png", "cover_too_large", make([]byte, MaxBytes+1)},
		{"dimension", "image/png", "validation_error", largeHeader(8193, 1)},
		{"pixels", "image/png", "validation_error", largeHeader(4000, 4000)},
	} {
		t.Run(test.name, func(t *testing.T) {
			_, err := Validate(test.data, test.kind)
			var problem *fault.Error
			if !errors.As(err, &problem) || problem.Code != test.code {
				t.Fatalf("want %s, got %v", test.code, err)
			}
		})
	}
}
