// Package media validates bounded raster images without external services.
package media

import (
	"bytes"
	"image"
	_ "image/jpeg"
	_ "image/png"
	"mime"

	"animemo.local/server/internal/fault"
)

const MaxBytes = 2 * 1024 * 1024

type Image struct {
	ContentType   string
	Width, Height int
	Data          []byte
}

func Validate(data []byte, contentType string) (Image, error) {
	if len(data) > MaxBytes {
		return Image{}, fault.New("cover_too_large", "封面不能超过 2 MB。")
	}
	kind, _, err := mime.ParseMediaType(contentType)
	if err != nil || (kind != "image/jpeg" && kind != "image/png") {
		return Image{}, fault.New("unsupported_media_type", "请选择 JPG 或 PNG 图片。")
	}
	config, format, err := image.DecodeConfig(bytes.NewReader(data))
	if err != nil || "image/"+format != kind {
		return Image{}, fault.Field("cover", "图片无效或与文件格式不符，请重新选择。")
	}
	if config.Width < 1 || config.Height < 1 || config.Width > 8192 || config.Height > 8192 || int64(config.Width)*int64(config.Height) > 12000000 {
		return Image{}, fault.Field("cover", "图片最长边不能超过 8192 像素，总像素不能超过 1200 万。")
	}
	// Decode the entire image only after bounding its allocation; a valid header
	// alone does not establish that the uploaded image is readable.
	if _, _, err := image.Decode(bytes.NewReader(data)); err != nil {
		return Image{}, fault.Field("cover", "图片内容损坏，请重新选择。")
	}
	return Image{ContentType: kind, Width: config.Width, Height: config.Height, Data: data}, nil
}
