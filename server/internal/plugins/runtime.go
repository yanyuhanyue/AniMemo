// Package plugins owns package identity, activation and the WASI sandbox.
// Core business modules never call this package or execute plugin callbacks.
package plugins

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"regexp"
	"slices"
	"strings"
	"time"
	"unicode/utf8"

	"animemo.local/server/internal/fault"
	"animemo.local/server/pkg/pluginproto"
	"github.com/tetratelabs/wazero"
	"github.com/tetratelabs/wazero/imports/wasi_snapshot_preview1"
)

// API 1 accepts the original journal conversion. API 2 also accepts import
// provenance fields; API 3 adds a declarative notes theme with no executable module.
const (
	HostAPI         = 3
	MinHostAPI      = 1
	MaxPackageBytes = 12 << 20
	MaxModuleBytes  = 8 << 20
	MaxInputBytes   = 2 << 20
	MaxOutputBytes  = 8 << 20
	RunTimeout      = 5 * time.Second
)

var slugPattern = regexp.MustCompile(`^[a-z][a-z0-9-]{0,47}$`)
var versionPattern = regexp.MustCompile(`^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$`)
var hashPattern = regexp.MustCompile(`^[a-f0-9]{64}$`)

func invalid(message string) error { return fault.New("validation_error", message) }

func strictJSON(data []byte, out any) error {
	if !utf8.Valid(data) {
		return invalid("插件数据必须为 UTF-8 JSON。")
	}
	d := json.NewDecoder(bytes.NewReader(data))
	d.DisallowUnknownFields()
	if err := d.Decode(out); err != nil {
		return invalid("插件数据格式无效或包含未声明字段。")
	}
	if d.Decode(new(any)) != io.EOF {
		return invalid("插件数据只能包含一个 JSON 对象。")
	}
	return nil
}

func Compatible(m pluginproto.Manifest) error {
	if m.Schema != 1 || m.Protocol != pluginproto.Version || m.HostMin < MinHostAPI || m.HostMin > HostAPI || m.HostMax < MinHostAPI || m.HostMin > m.HostMax {
		return invalid("插件协议或宿主版本范围不兼容。")
	}
	if len(m.Capabilities) != 1 {
		return invalid("每个扩展只能声明一种受支持的能力。")
	}
	switch m.Capabilities[0] {
	case "import.convert":
		if m.NotesTheme != nil {
			return invalid("文件转换器不能包含主题声明。")
		}
	case "theme.notes":
		if m.HostMin < 3 {
			return invalid("札记主题需要宿主接口 3。")
		}
		return validateNotesTheme(m.NotesTheme)
	default:
		return invalid("不支持此扩展能力。")
	}
	return nil
}

func ParsePackage(data []byte) (pluginproto.Package, string, error) {
	var p pluginproto.Package
	if len(data) > MaxPackageBytes {
		return p, "", fault.New("import_too_large", "插件包不能超过 12 MiB。")
	}
	if err := strictJSON(data, &p); err != nil {
		return p, "", err
	}
	m := p.Manifest
	if !slugPattern.MatchString(m.Slug) || !versionPattern.MatchString(m.Version) || strings.TrimSpace(m.Name) == "" || len([]rune(m.Name)) > 60 || len([]rune(m.Description)) > 800 || !hashPattern.MatchString(m.ModuleSHA256) {
		return p, "", invalid("插件名称、标识、版本或摘要无效。")
	}
	if err := Compatible(m); err != nil {
		return p, "", err
	}
	if len(p.Module) > MaxModuleBytes || fmt.Sprintf("%x", sha256.Sum256(p.Module)) != m.ModuleSHA256 {
		return p, "", invalid("模块大小或 SHA-256 校验失败。")
	}
	if m.NotesTheme != nil {
		if len(p.Module) != 0 {
			return p, "", invalid("声明式主题不能包含可执行模块。")
		}
		p.Module = []byte{}
	} else if len(p.Module) == 0 {
		return p, "", invalid("文件转换器缺少模块。")
	}
	canonical, _ := json.Marshal(m)
	return p, fmt.Sprintf("%x", sha256.Sum256(canonical)), nil
}

func validatePackageRuntime(ctx context.Context, m pluginproto.Manifest, module []byte) error {
	if m.NotesTheme != nil {
		if len(module) != 0 {
			return invalid("声明式主题不能包含可执行模块。")
		}
		return validateNotesTheme(m.NotesTheme)
	}
	return ValidateModule(ctx, module)
}

func runtimeConfig() wazero.RuntimeConfig {
	// No JIT/code cache, custom host functions, inherited environment, mounts or sockets.
	// 2048 WebAssembly pages = 128 MiB guest linear memory, per invocation.
	return wazero.NewRuntimeConfigInterpreter().WithMemoryLimitPages(2048).WithCloseOnContextDone(true)
}

func compile(ctx context.Context, rt wazero.Runtime, module []byte) (wazero.CompiledModule, error) {
	compiled, err := rt.CompileModule(ctx, module)
	if err != nil {
		return nil, invalid("无法加载 WebAssembly 模块。")
	}
	for _, f := range compiled.ImportedFunctions() {
		name, function, _ := f.Import()
		if name != wasi_snapshot_preview1.ModuleName {
			compiled.Close(ctx)
			return nil, invalid("插件不能导入宿主函数。")
		}
		provider := rt.Module(name)
		if provider == nil || provider.ExportedFunctionDefinitions()[function] == nil {
			compiled.Close(ctx)
			return nil, invalid("插件引用了当前宿主不支持的 WASI 函数。")
		}
		actual := provider.ExportedFunctionDefinitions()[function]
		if !slices.Equal(f.ParamTypes(), actual.ParamTypes()) || !slices.Equal(f.ResultTypes(), actual.ResultTypes()) {
			compiled.Close(ctx)
			return nil, invalid("插件 WASI 函数签名与当前宿主不兼容。")
		}
	}
	if len(compiled.ImportedMemories()) != 0 || compiled.ExportedFunctions()["_start"] == nil {
		compiled.Close(ctx)
		return nil, invalid("插件必须提供 WASI _start 入口，且不能导入共享内存。")
	}
	entry := compiled.ExportedFunctions()["_start"]
	if len(entry.ParamTypes()) != 0 || len(entry.ResultTypes()) != 0 {
		compiled.Close(ctx)
		return nil, invalid("插件 _start 入口不能带参数或返回值。")
	}
	return compiled, nil
}

func ValidateModule(ctx context.Context, module []byte) error {
	ctx, cancel := context.WithTimeout(ctx, RunTimeout)
	defer cancel()
	rt := wazero.NewRuntimeWithConfig(ctx, runtimeConfig())
	defer rt.Close(context.Background())
	if _, err := wasi_snapshot_preview1.Instantiate(ctx, rt); err != nil {
		return err
	}
	_, err := compile(ctx, rt, module)
	return err
}

type cappedWriter struct {
	bytes.Buffer
	limit    int
	exceeded bool
}

func (w *cappedWriter) Write(p []byte) (int, error) {
	if len(p) > w.limit-w.Len() {
		w.exceeded = true
		return 0, errors.New("output limit")
	}
	return w.Buffer.Write(p)
}

func Execute(ctx context.Context, module []byte, input pluginproto.Request) (pluginproto.Response, error) {
	var out pluginproto.Response
	if input.Protocol != pluginproto.Version || len(input.Text) == 0 || len(input.Text) > MaxInputBytes || !utf8.ValidString(input.Text) || strings.ContainsRune(input.Text, 0) || len(input.Filename) > 200 || strings.ContainsAny(input.Filename, "/\\\x00") {
		return out, invalid("请选择不超过 2 MiB 的 UTF-8 文本文件。")
	}
	ctx, cancel := context.WithTimeout(ctx, RunTimeout)
	defer cancel()
	rt := wazero.NewRuntimeWithConfig(ctx, runtimeConfig())
	defer rt.Close(context.Background())
	if _, err := wasi_snapshot_preview1.Instantiate(ctx, rt); err != nil {
		return out, err
	}
	compiled, err := compile(ctx, rt, module)
	if err != nil {
		return out, err
	}
	request, _ := json.Marshal(input)
	stdout := &cappedWriter{limit: MaxOutputBytes}
	stderr := &cappedWriter{limit: 4096}
	_, err = rt.InstantiateModule(ctx, compiled, wazero.NewModuleConfig().WithStdin(bytes.NewReader(request)).WithStdout(stdout).WithStderr(stderr))
	// Guest diagnostics never enter server logs: they may contain private input.
	if ctx.Err() != nil {
		return out, fault.New("plugin_failed", "插件运行超时或已取消；手账未修改。")
	}
	if err != nil || stdout.exceeded || stderr.exceeded {
		return out, fault.New("plugin_failed", "插件运行失败或输出超限；手账未修改。")
	}
	if err := strictJSON(stdout.Bytes(), &out); err != nil {
		return out, fault.New("plugin_failed", "插件返回了无效结果；手账未修改。")
	}
	if out.Protocol != pluginproto.Version {
		return out, fault.New("plugin_failed", "插件响应协议不兼容。")
	}
	if out.Error != "" {
		if len([]rune(out.Error)) > 400 {
			return out, fault.New("plugin_failed", "插件无法解析该文件。")
		}
		return out, fault.New("plugin_failed", out.Error)
	}
	if (out.Format != "json" && out.Format != "csv") || len(out.Data) == 0 || len(out.Data) > 4<<20 {
		return out, fault.New("plugin_failed", "插件必须返回不超过 4 MiB 的 JSON 或 CSV 导入文件。")
	}
	return out, nil
}
