package plugins

import (
	"animemo.local/server/internal/fault"
	"animemo.local/server/pkg/pluginproto"
	"bytes"
	"context"
	"encoding/json"
	"io"
	"os/exec"
	"time"
)

type Executor func(context.Context, []byte, pluginproto.Request) (pluginproto.Response, error)
type Invocation struct {
	Module  []byte              `json:"module"`
	Request pluginproto.Request `json:"request"`
}

// The production executor receives only module bytes and the explicitly selected
// file. Database credentials, host environment and filesystem mounts are absent.
func ProcessExecutor(executable string) Executor {
	return func(ctx context.Context, module []byte, input pluginproto.Request) (pluginproto.Response, error) {
		ctx, cancel := context.WithTimeout(ctx, RunTimeout+2*time.Second)
		defer cancel()
		data, err := json.Marshal(Invocation{Module: module, Request: input})
		if err != nil {
			return pluginproto.Response{}, err
		}
		cmd := exec.CommandContext(ctx, executable, "plugin-exec")
		cmd.Env = []string{"GOMEMLIMIT=192MiB", "GOMAXPROCS=1"}
		cmd.Dir = "/"
		cmd.Stdin = bytes.NewReader(data)
		stdout := &cappedWriter{limit: MaxOutputBytes + 4096}
		stderr := &cappedWriter{limit: 4096}
		cmd.Stdout = stdout
		cmd.Stderr = stderr
		cmd.WaitDelay = time.Second
		if err = cmd.Run(); err != nil || stdout.exceeded || stderr.exceeded {
			return pluginproto.Response{}, fault.New("plugin_failed", "扩展进程运行失败或超时；手账未修改。")
		}
		var response pluginproto.Response
		if err = strictJSON(stdout.Bytes(), &response); err != nil {
			return response, fault.New("plugin_failed", "扩展进程返回无效结果。")
		}
		if response.Error != "" {
			return response, fault.New("plugin_failed", response.Error)
		}
		return response, nil
	}
}

func ServeProcess(in io.Reader, out io.Writer) error {
	if err := limitProcess(); err != nil {
		return err
	}
	data, err := io.ReadAll(io.LimitReader(in, MaxPackageBytes+MaxInputBytes+4097))
	if err != nil {
		return err
	}
	if len(data) > MaxPackageBytes+MaxInputBytes+4096 {
		return invalid("扩展输入超限。")
	}
	var request Invocation
	if err = strictJSON(data, &request); err != nil {
		return err
	}
	if len(request.Module) > MaxModuleBytes {
		return invalid("扩展模块超限。")
	}
	response, err := Execute(context.Background(), request.Module, request.Request)
	if err != nil {
		response = pluginproto.Response{Protocol: pluginproto.Version, Error: "扩展无法处理该文件，请检查日期和文本格式。"}
	}
	return json.NewEncoder(out).Encode(response)
}
