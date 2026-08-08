class TemplatePlugin:
    def __init__(self, host):
        self.host = host
        host.api.get("status", handler=self.status, access="user")
        host.integrations.register_action("echo", self.echo)

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

    def echo(self, context, payload):
        return {
            "user_id": context.user.pk,
            "username": context.user.get_username(),
            "payload": payload,
        }


def create_plugin(host):
    return TemplatePlugin(host)
