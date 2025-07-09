import execjs


class SignTool:

    def __init__(self):
        js_path = "static/xianyu_js_version_2.js"
        with open(js_path, "r", encoding="utf-8") as f:
            self.vm = execjs.compile(f.read())

    def generate_mid(self):
        mid = self.vm.call("generate_mid")
        return mid

    def generate_uuid(self):
        uuid = self.vm.call("generate_uuid")
        return uuid

    def generate_device_id(self, user_id):
        device_id = self.vm.call("generate_device_id", user_id)
        return device_id

    def generate_sign(self, t, token, data):
        sign = self.vm.call("generate_sign", t, token, data)
        return sign

    def decrypt(self, data):
        res = self.vm.call("decrypt", data)
        return res
