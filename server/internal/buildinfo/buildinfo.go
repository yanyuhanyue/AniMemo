// Package buildinfo identifies a built application without contacting a database
// or importing release tooling. Values are supplied by the build entry points.
package buildinfo

import "animemo.local/server/internal/database"

var Version = "development"
var Revision = "unknown"

type Info struct {
	Version    string               `json:"version"`
	Revision   string               `json:"revision"`
	Migrations []database.Migration `json:"migrations"`
}

func Current() (Info, error) {
	migrations, err := database.Manifest()
	return Info{Version: Version, Revision: Revision, Migrations: migrations}, err
}
