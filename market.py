"""AutoDL ordinary marketplace adapter. All remote traffic uses the logged-in browser."""
import asyncio
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

ORIGIN = "https://www.autodl.com"
MARKET = ORIGIN + "/market/list"
CONSOLE = ORIGIN + "/console/instance/list"


class Pause(Exception):
    """User action or a service contract check is needed."""


class SoldOut(Exception):
    pass


def base_images(nodes, path=()):
    for node in nodes:
        label = node["label_name"]
        if isinstance(label, str):
            yield from base_images(node.get("children", []), (*path, label))
        elif label.get("uuid"):
            yield {"id": "base:" + label["uuid"], "label": " / ".join((*path, str(label["v"]))),
                   "cuda": label["v"], "arch": label.get("cpu_arch"), "chip": label.get("chip_corp")}


def version(value):
    match = re.fullmatch(r"\s*(\d+)(?:\.(\d+))?(?:\.\d+)?\s*", str(value))
    return (int(match[1]), int(match[2] or 0)) if match else None


def eligible(host, count, img, series, min_expand_bytes=0):
    if min_expand_bytes > 0 and int(host.get("max_data_disk_expand_size") or 0) < min_expand_bytes:
        return False
    if (host["gpu_idle_num"] < count
            or host["on_line"] in (3, 4, 5, 6)
            or host["max_instance_num"] <= host["binding_instance_num"]):
        return False
    mode = host.get("rent_mode")
    if mode:
        step = re.search(r"\d+", mode)
        if not step or int(step[0]) == 0:
            return False
        minimum = int(step[0])
        if host["gpu_idle_num"] >= minimum:
            if count < minimum or (not mode.startswith("Base") and count % minimum):
                return False
    if img["arch"] != host["cpu_arch"] or img["chip"] != host["chip_corp"]:
        return False
    if img["chip"] == "nvidia" and img["cuda"]:
        requested, highest = version(img["cuda"]), version(host.get("highest_cuda_version"))
        floor = {"NVIDIA-Ampere": (11, 0), "NVIDIA-Ada": (11, 3),
                 "NVIDIA-Blackwell": (12, 8)}.get(series, (0, 0))
        return bool(requested and highest and floor <= requested <= highest)
    if img["chip"] == "mthreads":
        required = {"2.7.0": "3.1.0", "5.2.0-server": "5.2.0"}.get(host.get("driver_version"))
        return not required or str(img["cuda"]) == required
    return True


def price_request(host, count):
    return {"machine_id": host["machine_id"], "charge_type": "payg", "duration": 1,
            "num": count, "coupon_id_list": [], "expand_data_disk": 0}


def _first(mapping, *names):
    for name in names:
        value = mapping.get(name)
        if value not in (None, ""):
            return value
    return None


def _clone_source(data):
    """Normalize fields used by AutoDL's clone order from a snapshot."""
    if not isinstance(data, dict):
        return {}
    source = data.get("instance_info") or data.get("instance") or data
    if not isinstance(source, dict):
        source = {}
    nested = source.get("image") if isinstance(source.get("image"), dict) else {}
    machine = source.get("machine_info_snapshot") or data.get("machine_info_snapshot")
    if not isinstance(machine, dict):
        machine = {}
    merged = {**data, **machine, **nested, **source}
    application_info = _first(merged, "cg_application_info")
    if not isinstance(application_info, dict):
        application_info = {"image_name": "", "current_version_id": 0,
                            "current_version": "", "image_id": 0}
    base_image = _first(merged, "base_image_uuid", "base_image", "image_uuid")
    if not base_image and merged.get("image_type") == "public":
        base_image = _first(merged, "image_name")
    expand_data_disk = _first(merged, "expand_data_disk_size", "data_disk_expand_size", "expand_data_disk") or 0
    try:
        expand_data_disk = int(expand_data_disk)
    except (TypeError, ValueError):
        expand_data_disk = 0
    if 0 < expand_data_disk < 1024 ** 3:
        expand_data_disk *= 1024 ** 3
    return {
        "base_image_uuid": base_image or "",
        "private_image_uuid": _first(merged, "private_image_uuid", "private_image") or "",
        "reproduction_uuid": _first(merged, "reproduction_uuid") or "",
        "cg_application_uuid": _first(merged, "cg_application_uuid") or "",
        "cg_application_info": application_info,
        "reproduction_id": _first(merged, "reproduction_id") or 0,
        "ft_sparse": bool(_first(merged, "ft_sparse")),
        "arch": _first(merged, "cpu_arch", "arch", "architecture"),
        "chip": _first(merged, "chip_corp", "chip", "chip_company"),
        "cuda": _first(merged, "cuda_v", "image_cuda", "cuda_version", "cuda"),
        "gpu": _first(merged, "gpu_name", "gpu_type_name", "gpu"),
        "expand_data_disk": expand_data_disk,
        "region_sign": _first(merged, "region_sign"),
    }


def host_cpu(host):
    base = host.get("machine_base_info") if isinstance(host.get("machine_base_info"), dict) else {}
    name = _first(base, "cpu_name") or _first(host, "cpu_name")
    cores = _first(host, "cpu_per_gpu", "cpu_num")
    try:
        cores = int(cores) if cores is not None else None
    except (TypeError, ValueError):
        cores = None
    return name, cores


def cpu_matches(host, settings):
    names = settings.get("cpu_names") or []
    cores = settings.get("cpu_cores") or []
    name, per_gpu = host_cpu(host)
    if names and not name:
        return False
    if names:
        actual = " ".join(str(name).split()).casefold()
        requested = [" ".join(str(value).split()).casefold() for value in names]
        if not any(value == actual or value in actual or actual in value for value in requested):
            return False
    return not cores or per_gpu in {int(value) for value in cores}


# Credentials stay on AutoDL's origin, including when the user navigates the login tab.
REQUEST = (Path(__file__).parent / "extension/request.js").read_text(encoding="utf-8")


class Market:
    def __init__(self, directory: Path):
        self.directory = directory
        self.driver = self.context = self.page = None
        self.bridge = None
        self.browser_lock = asyncio.Lock()

    async def login(self):
        from playwright.async_api import async_playwright

        async with self.browser_lock:
            if not self.context:
                if not self.driver:
                    self.driver = await async_playwright().start()
                edge = next((Path(root) / "Microsoft/Edge/Application/msedge.exe"
                             for var in ("ProgramFiles(x86)", "ProgramFiles")
                             if (root := os.environ.get(var))
                             and (Path(root) / "Microsoft/Edge/Application/msedge.exe").is_file()), None)
                self.context = await self.driver.chromium.launch_persistent_context(
                    str(self.directory), headless=False, no_viewport=True,
                    executable_path=str(edge) if edge else None,
                    ignore_default_args=["--enable-automation"],
                    args=["--disable-blink-features=AutomationControlled"])
                await self.context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                self.context.on("close", self._closed)
            if not self.page or self.page.is_closed():
                self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            await self.page.goto(MARKET, wait_until="domcontentloaded")
            if not await self.page.evaluate("Boolean(localStorage.getItem('token'))"):
                await self.page.goto(ORIGIN + "/login?url=%2Fmarket%2Flist", wait_until="domcontentloaded")
            await self.page.bring_to_front()
            if self.bridge:
                self.bridge.disconnect()
                self.bridge = None

    def _closed(self, *_):
        self.context = self.page = None

    async def close(self):
        if self.context:
            await self.context.close()
        if self.driver:
            await self.driver.stop()

    async def call(self, endpoint, body=None, account=None):
        if self.bridge:
            result = await self.bridge.call(endpoint, body, account)
        else:
            if not self.page or self.page.is_closed():
                raise Pause("请打开专用浏览器登录，或从已登录的 AutoDL 标签页连接本地扩展。")
            result = await self.page.evaluate("(" + REQUEST + ")", {"endpoint": endpoint, "body": body, "account": account})
        if result["code"] == "MachineGpuNumUpdateFailed":
            raise SoldOut()
        if result["code"] != "Success":
            raise Pause(str(result.get("msg") or result["code"])[:180])
        return result

    async def models(self):
        reply = await self.call("machine/gpu_type")
        return reply["account"], {g["gpu_name"]: g for g in reply["data"]}

    async def images(self, gpu, account):
        filters = {"cpu_arch": [gpu["cpu_arch"]], "chip_corp": [gpu["chip_corp"]]}
        public = (await self.call("image/all", filters, account))["data"]
        private = (await self.call("image/private/get", filters, account))["data"]
        images = list(base_images(public or []))
        for item in private or []:
            images.append({"id": "private:" + item["image_uuid"], "label": "我的镜像 / " + item["name"],
                           "cuda": item.get("cuda_v"), "arch": item.get("cpu_arch"),
                           "chip": item.get("chip_corp")})
        return images

    async def image_catalogs(self, names, models, account):
        catalogs = {}
        for name in names:
            gpu = models[name]
            architecture = (gpu["chip_corp"], gpu["cpu_arch"])
            if architecture not in catalogs:
                catalogs[architecture] = {i["id"]: i for i in await self.images(gpu, account)}
        return catalogs

    async def cpu_options(self, names, account):
        query = {"charge_type": "payg", "region_sign": "", "region_sign_list": [],
                 "gpu_type_name": list(names), "machine_tag_name": [], "gpu_idle_num": 0,
                 "mount_net_disk": False, "cpu_arch": [], "chip_corp": [],
                 "default_order": True, "page_index": 1, "page_size": 90}
        models, cores = set(), set()
        while True:
            data = (await self.call("user/machine/list", query, account))["data"] or {}
            for host in data.get("list") or []:
                name, per_gpu = host_cpu(host)
                if name:
                    models.add(str(name))
                if per_gpu:
                    cores.add(per_gpu)
            if not data.get("list") or query["page_index"] * 90 >= int(data.get("result_total") or 0):
                break
            query["page_index"] += 1
        return sorted(models), sorted(cores)

    async def instances(self):
        """Return safe source-instance choices for the clone workflow."""
        account, _ = await self.models()
        page = 1
        rows = []
        while True:
            payload = {"page_index": page, "page_size": 50, "name": "", "status": [], "charge_type": []}
            data = (await self.call("instance", payload, account))["data"] or {}
            for row in data.get("list") or []:
                uuid = row.get("uuid")
                if not uuid:
                    continue
                name = row.get("name") or uuid
                gpu = _first(row, "gpu_name", "gpu_type_name", "gpu") or "GPU"
                status = row.get("status") or row.get("sub_status") or ""
                rows.append({"id": uuid, "label": "{} · {}{}".format(
                    name, gpu, " · " + str(status) if status else "")})
            total = int(data.get("result_total") or 0)
            if not data.get("list") or page * 50 >= total:
                break
            page += 1
        return account, rows

    async def clone_source(self, uuid, account):
        """Read clone metadata from the source instance snapshot endpoint."""
        endpoint = "instance/snapshot?instance_uuid=" + quote(str(uuid), safe="")
        reply = await self.call(endpoint, None, account)
        return _clone_source(reply.get("data"))

    @staticmethod
    def _clone_eligible(host, count, source, series, min_expand_bytes=0):
        required_disk = int(source.get("expand_data_disk") or 0)
        max_expand = int(host.get("max_data_disk_expand_size") or 0)
        if required_disk and max_expand < required_disk:
            return False
        if min_expand_bytes > 0 and int(host.get("max_data_disk_expand_size") or 0) < min_expand_bytes:
            return False
        if (host["gpu_idle_num"] < count
                or host["on_line"] in (3, 4, 5, 6)
                or host["max_instance_num"] <= host["binding_instance_num"]):
            return False
        mode = host.get("rent_mode")
        if mode:
            step = re.search(r"\d+", mode)
            if not step or int(step[0]) == 0:
                return False
            minimum = int(step[0])
            if host["gpu_idle_num"] >= minimum:
                if count < minimum or (not mode.startswith("Base") and count % minimum):
                    return False
        arch, chip, cuda = source.get("arch"), source.get("chip"), source.get("cuda")
        if arch and arch != host.get("cpu_arch"):
            return False
        if chip and chip != host.get("chip_corp"):
            return False
        if chip == "nvidia" and cuda:
            requested, highest = version(cuda), version(host.get("highest_cuda_version"))
            floor = {"NVIDIA-Ampere": (11, 0), "NVIDIA-Ada": (11, 3),
                     "NVIDIA-Blackwell": (12, 8)}.get(series, (0, 0))
            return bool(requested and highest and floor <= requested <= highest)
        if chip == "mthreads" and cuda:
            required = {"2.7.0": "3.1.0", "5.2.0-server": "5.2.0"}.get(host.get("driver_version"))
            return not required or str(cuda) == required
        return True

    async def candidate(self, settings):
        account, models = await self.models()
        if account != settings["account"]:
            raise Pause("登录账号已变化。")
        min_expand = 0
        if settings.get("disk"):
            min_expand = int(Decimal(str(settings["disk"])) * 1024 ** 3)
        selected = settings["gpu"]
        names = [name for name in ([selected] if isinstance(selected, str) else selected) if name in models]
        if not names:
            raise Pause("所选型号已从市场移除，请重新读取选项。")
        clone_source = None
        if settings.get("clone_instance"):
            clone_source = await self.clone_source(settings["clone_instance"], account)
            if not settings.get("copy_data_disk"):
                clone_source = {**clone_source, "expand_data_disk": 0}
        if clone_source is None:
            catalogs = await self.image_catalogs(names, models, account)
            images = {}
            for name in names:
                gpu = models[name]
                image = catalogs[(gpu["chip_corp"], gpu["cpu_arch"])].get(settings["image"])
                if image:
                    images[name] = image
            if not images:
                raise Pause("所选镜像对全部候选型号均不可用，请重新选择。")
        query = {"charge_type": "payg", "region_sign": "", "region_sign_list": [],
                 "gpu_type_name": names, "machine_tag_name": [],
                 "gpu_idle_num": settings["count"], "mount_net_disk": False,
                 "cpu_arch": [], "chip_corp": [], "default_order": True,
                 "page_index": 1, "page_size": 90}
        if clone_source and clone_source.get("region_sign"):
            query["region_sign_list"] = [clone_source["region_sign"]]
        while True:
            data = (await self.call("user/machine/list", query, account))["data"]
            for host in data["list"] or []:
                name = host["gpu_name"]
                if clone_source and clone_source.get("region_sign") and host.get("region_sign") \
                        and host["region_sign"] != clone_source["region_sign"]:
                    continue
                if not cpu_matches(host, settings):
                    continue
                if clone_source is None:
                    if name not in images or not eligible(
                            host, settings["count"], images[name], models[name].get("gpu_series_type"), min_expand):
                        continue
                elif not self._clone_eligible(
                        host, settings["count"], clone_source,
                        models[name].get("gpu_series_type"), min_expand):
                    continue
                price = price_request(host, settings["count"])
                if clone_source is not None:
                    price["expand_data_disk"] = clone_source.get("expand_data_disk", 0)
                quote = (await self.call("order/price/preview", price, account))["data"]
                # Undiscounted one-hour quote for the requested quantity is a conservative ceiling.
                try:
                    hourly = Decimal(str(quote["origin_price"])) / 1000
                except (InvalidOperation, TypeError, KeyError):
                    continue
                if not hourly.is_finite() or hourly <= 0:
                    continue
                if settings["limit"] is not None and hourly > Decimal(settings["limit"]):
                    continue
                cpu_name, cpu_per_gpu = host_cpu(host)
                result = {"gpu": name, "machine": host["machine_id"], "price": str(hourly),
                          "cpu_name": cpu_name or "", "cpu_per_gpu": cpu_per_gpu or 0,
                          "price_info": price}
                if clone_source is not None:
                    result["clone_source"] = clone_source
                return result
            if not data["list"] or query["page_index"] * 90 >= data["result_total"]:
                return None
            query["page_index"] += 1

    async def submit(self, settings, candidate, name):
        source = candidate.get("clone_source")
        if source is None:
            kind, uuid = settings["image"].split(":", 1)
            instance = {"machine_id": candidate["machine"], "charge_type": "payg",
                        "req_gpu_amount": settings["count"], "instance_name": name,
                        "base_image_uuid": uuid if kind == "base" else "",
                        "private_image_uuid": uuid if kind == "private" else "",
                        "reproduction_uuid": "", "cg_application_uuid": "", "reproduction_id": 0,
                        "cg_application_info": {"image_name": "", "current_version_id": 0,
                                                "current_version": "", "image_id": 0},
                        "expand_data_disk": 0}
            endpoint = "order/instance/create/payg"
            body = {"instance_info": instance, "price_info": candidate["price_info"]}
        else:
            instance = {"machine_id": candidate["machine"], "charge_type": "payg",
                        "req_gpu_amount": settings["count"], "instance_name": name,
                        "base_image_uuid": source.get("base_image_uuid", ""),
                        "private_image_uuid": source.get("private_image_uuid", ""),
                        "reproduction_uuid": source.get("reproduction_uuid", ""),
                        "cg_application_uuid": source.get("cg_application_uuid", ""),
                        "cg_application_info": source.get("cg_application_info") or {
                            "image_name": "", "current_version_id": 0,
                            "current_version": "", "image_id": 0},
                        "expand_data_disk": source.get("expand_data_disk", 0),
                        "copy_data_disk_after_clone": bool(settings.get("copy_data_disk")),
                        "keep_src_user_service_address_after_clone": False,
                        "reproduction_id": int(source.get("reproduction_id") or 0),
                        "ft_sparse": bool(source.get("ft_sparse"))}
            endpoint = "order/instance/clone/payg"
            body = {"instance_uuid": settings["clone_instance"],
                    "instance_info": instance, "price_info": candidate["price_info"]}
        reply = await self.call(endpoint, body, settings["account"])
        uuid = reply["data"]
        if not isinstance(uuid, str) or not uuid.strip():
            raise Pause("创建响应未包含实例 ID。")
        return uuid

    async def reconcile(self, settings, name):
        page = 1
        while True:
            data = (await self.call("instance", {"page_index": page, "page_size": 50,
                                    "name": name, "status": [], "charge_type": []},
                                    settings["account"]))["data"]
            for row in data["list"] or []:
                if row["name"] == name and row.get("uuid"):
                    return row["uuid"]
            if not data["list"] or page * 50 >= data["result_total"]:
                return None
            page += 1
