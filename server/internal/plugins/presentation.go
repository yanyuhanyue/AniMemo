package plugins

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"io"
	"regexp"
	"strings"
	"unicode/utf8"

	"animemo.local/server/internal/media"
	"animemo.local/server/pkg/pluginproto"
	"animemo.local/server/pkg/themeproto"
	"github.com/tdewolff/parse/v2"
	"github.com/tdewolff/parse/v2/css"
	"golang.org/x/net/html"
	"golang.org/x/net/html/atom"
)

var assetName = regexp.MustCompile(`^[a-z][a-z0-9.-]{0,63}$`)
var themeClass = regexp.MustCompile(`^[a-zA-Z][a-zA-Z0-9 _-]{0,159}$`)
var assetURL = regexp.MustCompile(`^url\(["']?asset:([a-z][a-z0-9.-]{0,63})["']?\)$`)
var templateTags = strings.Fields("div section article header footer aside figure figcaption span p h3 h4 strong em small ul li br hr slot img")

func includes(values []string, value string) bool {
	for _, v := range values {
		if value == v {
			return true
		}
	}
	return false
}

func validatePresentation(p *themeproto.Presentation) error {
	if p == nil {
		return nil
	}
	if p.Schema != 1 || p.Scope != "private.notes" || len(p.Assets) > 8 {
		return invalid("主题模板版本、作用范围或资源数量无效。")
	}
	assets := map[string]string{}
	fonts := 0
	for _, a := range p.Assets {
		if !assetName.MatchString(a.Name) || assets[a.Name] != "" || !hashPattern.MatchString(a.SHA256) || !includes([]string{"image/png", "image/jpeg", "font/woff2", "text/plain"}, a.ContentType) {
			return invalid("主题资源声明无效。")
		}
		assets[a.Name] = a.ContentType
		if a.ContentType == "font/woff2" {
			fonts++
		}
		if fonts > 1 {
			return invalid("当前主题模板支持一个随包 WOFF2 字体。")
		}
	}
	for _, spec := range []struct {
		text               string
		required, optional []string
	}{
		{p.List, []string{"items"}, nil},
		{p.Card, []string{"title", "excerpt", "metadata", "read"}, []string{"poster", "work", "picture"}},
		{p.Reader, []string{"body", "metadata"}, []string{"work"}},
	} {
		if err := validateTemplate(spec.text, spec.required, spec.optional, assets); err != nil {
			return err
		}
	}
	return validateThemeCSS(p.CSS, assets)
}

// HTML is parsed with the standard HTML5 rules; only inert structural elements,
// classes, local decorative images and named core slots are accepted.
func validateTemplate(source string, required, optional []string, assets map[string]string) error {
	if len(source) == 0 || len(source) > 16<<10 {
		return invalid("主题 HTML 模板为空或超过 16 KiB。")
	}
	nodes, err := html.ParseFragment(strings.NewReader(source), &html.Node{Type: html.ElementNode, Data: "div", DataAtom: atom.Div})
	if err != nil {
		return invalid("主题 HTML 模板无法解析。")
	}
	slots := map[string]bool{}
	count := 0
	var visit func(*html.Node, int) error
	visit = func(n *html.Node, depth int) error {
		count++
		if count > 128 || depth > 12 {
			return invalid("主题模板层级或节点过多。")
		}
		if n.Type == html.TextNode {
			return nil
		}
		if n.Type != html.ElementNode || n.Namespace != "" || !includes(templateTags, n.Data) {
			return invalid("主题只允许结构元素、装饰图片及核心内容槽。")
		}
		attrs := map[string]string{}
		for _, a := range n.Attr {
			if a.Namespace != "" {
				return invalid("主题不允许命名空间属性。")
			}
			if _, ok := attrs[a.Key]; ok {
				return invalid("主题包含重复属性。")
			}
			attrs[a.Key] = a.Val
			if a.Key == "class" && themeClass.MatchString(a.Val) && n.Data != "slot" {
				continue
			}
			if n.Data == "slot" && a.Key == "name" {
				continue
			}
			if n.Data == "img" && (a.Key == "src" || a.Key == "alt") {
				continue
			}
			return invalid("主题元素属性不受支持；不允许脚本、链接、内联样式或事件。")
		}
		if n.Data == "slot" {
			name := attrs["name"]
			if (!includes(required, name) && !includes(optional, name)) || slots[name] || n.FirstChild != nil {
				return invalid("主题内容槽缺失、重复或不适用于此模板。")
			}
			slots[name] = true
		}
		if n.Data == "img" {
			src := attrs["src"]
			kind := assets[strings.TrimPrefix(src, "asset:")]
			alt, ok := attrs["alt"]
			if !strings.HasPrefix(src, "asset:") || !strings.HasPrefix(kind, "image/") || !ok || alt != "" {
				return invalid("主题图片只能引用包内装饰资源，并使用空 alt。")
			}
		}
		for child := n.FirstChild; child != nil; child = child.NextSibling {
			if err := visit(child, depth+1); err != nil {
				return err
			}
		}
		return nil
	}
	for _, n := range nodes {
		if err := visit(n, 0); err != nil {
			return err
		}
	}
	for _, name := range required {
		if !slots[name] {
			return invalid("主题模板缺少必要的核心内容槽：" + name)
		}
	}
	return nil
}

// CSS is parsed rather than sanitized with a selector/URL regular expression.
// Escapes, imports, arbitrary functions and host selectors are deliberately not
// part of v1. URLs must refer to declared package assets; custom properties can
// only hold equally validated values. Shadow DOM supplies selector isolation.
func validateThemeCSS(source string, assets map[string]string) error {
	if len(source) == 0 || len(source) > 32<<10 || strings.ContainsAny(source, "\\\x00<>") {
		return invalid("主题 CSS 为空、过大或包含不支持的转义。")
	}
	parser := css.NewParser(parse.NewInputString(source), false)
	functions := []string{"rgb(", "rgba(", "hsl(", "hsla(", "calc(", "min(", "max(", "clamp(", "var(", "repeat(", "minmax(", "fit-content(", "linear-gradient(", "repeating-linear-gradient(", "radial-gradient(", "rotate(", "translate(", "translatex(", "translatey(", "scale(", "scalex(", "scaley(", "cubic-bezier(", "steps("}
	for {
		grammar, _, data := parser.Next()
		if grammar == css.ErrorGrammar {
			if parser.Err() == io.EOF {
				return nil
			}
			return invalid("主题 CSS 语法无效。")
		}
		word := strings.ToLower(string(data))
		if grammar == css.AtRuleGrammar || grammar == css.BeginAtRuleGrammar {
			if grammar != css.BeginAtRuleGrammar || word != "@media" {
				return invalid("主题 CSS 只支持 media 规则；字体通过随包资源声明。")
			}
		}
		if grammar == css.DeclarationGrammar && (word == "position" || word == "behavior" || word == "-moz-binding" || word == "all") {
			if word != "position" {
				return invalid("主题 CSS 包含不支持的属性。")
			}
			value := ""
			for _, t := range parser.Values() {
				value += strings.ToLower(string(t.Data))
			}
			if strings.TrimSpace(value) != "relative" && strings.TrimSpace(value) != "static" && strings.TrimSpace(value) != "absolute" {
				return invalid("主题 CSS 不支持固定于视口的定位。")
			}
		}
		for _, token := range parser.Values() {
			raw := string(token.Data)
			lower := strings.ToLower(raw)
			if token.TokenType == css.IdentToken && includes([]string{"host", "host-context", "part", "slotted"}, lower) {
				return invalid("主题 CSS 不得选择宿主或外部组件。")
			}
			if token.TokenType == css.FunctionToken && !includes(functions, lower) {
				return invalid("主题 CSS 包含不支持的函数。")
			}
			if token.TokenType == css.URLToken {
				match := assetURL.FindStringSubmatch(raw)
				if len(match) != 2 || assets[match[1]] == "" {
					return invalid("主题 CSS 只能加载包内声明的资源。")
				}
			}
			if token.TokenType == css.BadURLToken || token.TokenType == css.BadStringToken {
				return invalid("主题 CSS 包含无效资源或字符串。")
			}
		}
	}
}

func validateThemeAssets(p pluginproto.Package) error {
	var specs []themeproto.Asset
	if p.Manifest.NotesTheme != nil && p.Manifest.NotesTheme.Presentation != nil {
		specs = p.Manifest.NotesTheme.Presentation.Assets
	}
	if len(specs) != len(p.Assets) {
		return invalid("主题资源文件与声明不一致。")
	}
	total := 0
	for _, spec := range specs {
		data, ok := p.Assets[spec.Name]
		total += len(data)
		if !ok || len(data) == 0 || len(data) > 2<<20 || total > 4<<20 || fmt.Sprintf("%x", sha256.Sum256(data)) != spec.SHA256 {
			return invalid("主题资源缺失、超量或摘要不匹配。")
		}
		if spec.ContentType == "text/plain" {
			if len(data) > 32<<10 || !utf8.Valid(data) || bytes.ContainsRune(data, 0) {
				return invalid("主题许可文本无效。")
			}
		} else if spec.ContentType == "font/woff2" {
			if len(data) < 48 || !bytes.Equal(data[:4], []byte("wOF2")) || binary.BigEndian.Uint32(data[8:12]) != uint32(len(data)) || binary.BigEndian.Uint32(data[16:20]) > 16<<20 {
				return invalid("主题 WOFF2 字体头或展开大小无效。")
			}
		} else if _, err := media.Validate(data, spec.ContentType); err != nil {
			return err
		}
	}
	return nil
}
