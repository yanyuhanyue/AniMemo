class TemplatePlugin:
    def __init__(self, host):
        self.host = host
        host.api.get("status", handler=self.status, access="user")

    def health_check(self):
        return {"status": "healthy", "version": self.host.version}

    def status(self, request):
        return {
            "status": "ok",
            "plugin": {
                "id": self.host.manifest["id"],
                "slug": self.host.slug,
                "version": self.host.version,
            },
        }


def create_plugin(host):
    return TemplatePlugin(host)
