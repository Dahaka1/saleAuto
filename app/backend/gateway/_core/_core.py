import json
import os
import warnings
from typing import Literal, Any, Self

from contextlib import asynccontextmanager
import httpx
from colorama import Fore
from fastapi import FastAPI, APIRouter, Request, HTTPException, status
from fastapi.responses import JSONResponse
from httpx import Response, AsyncClient

import config
import enums
from . import _conf, _exceptions
from ._utils import check_url_path, get_app_from_path, get_client_data_from_request, path_data_equal
from .._auth import authEnum
from pkg.logging import error, log


class AppPath:
	_urls_list: list[dict[str, Any]] = []

	def __init__(
		self,
		path: str,
		authType: authEnum | None,
		method: Literal["get", "put", "post", "delete"],
		app: enums.AppUrlEnum,
		id: int = None
	):
		check_url_path(path)
		self.id = id
		self.path = self._get_path_string(path, app)
		self.authType = authType
		self.method = method
		self.app = app.value
		self.request: Request | None = None
		self._expects: dict[str, list[str]] | None = None
		AppPath._urls_list = self.__all()
		if self.id:
			if self.id not in map(lambda obj: obj["id"], AppPath._urls_list):
				raise _exceptions.AppPathException(f"Got an not registered app path ID {self.id}")

	@staticmethod
	def _get_path_string(path: str, app: enums.AppUrlEnum) -> str:
		return f"{app.value}{path}"

	@classmethod
	async def from_request(cls, request: Request) -> Self:
		"""
		:param request: fastapi request
		:return: AppPath
		"""
		path = str(request.url)
		method = request.method
		app = get_app_from_path(path)
		data = await get_client_data_from_request(request)
		registered_path = cls.__get_path(cls._urls_list, path, method, data, app)
		if not registered_path:
			raise _exceptions.AppPathException(f"<URL '{path}'> "
											   f"Received data: {data} "
											   f"No matches with any of registered app paths")
		registered_path.request = request
		return registered_path

	@classmethod
	def __get_path(cls, urls: list[dict[str, Any]], path: str, method: str,
				   data: dict[str, list[str]], app: enums.AppUrlEnum) -> Self | None:
		for url in urls:
			url_, method_, app_, data_ = url["path"], url["method"], url["app"], url["data"]
			if url_ in path and str(method).lower() == method_ and app.value == app_:
				if data_ and any(data_) and not path_data_equal(request_data=data, expected_data=data_):
					continue
				auth_type = url.get("authType")
				login_type = authEnum(auth_type) if auth_type else None
				obj = cls(url_, login_type, method_, enums.AppUrlEnum(app_), id=url["id"])
				obj.registered = True
				return obj

	def get_url(self, url: str) -> str:
		app_url = self.app.lstrip("/").rstrip("/").upper()
		template = f"APP_{app_url}_%s"
		try:
			host = getattr(config, template % "HOST")
			port = getattr(config, template % "PORT")
		except AttributeError:
			raise _exceptions.AppNotFound(f"{self} host & port (or or) not defined at virtual environment")
		url = f"http://{host}:{port}{config.GLOBAL_PREFIX}{self.app}{url}"
		return url

	@staticmethod
	def __all() -> list[dict[str, Any]]:
		registered_urls = os.getenv(_conf.ENV_URLS_PATH)
		if not registered_urls:
			return []
		return json.loads(registered_urls)

	def dict(self) -> dict[str, Any]:
		dict_ = dict(
			path=self.path,
			app=self.app,
			method=self.method,
			authType=self.authType.value if self.authType else None,
			data=self._expects,
			id=self.id
		)
		if self.request:
			dict_["request"] = self.request
		return dict_

	def _update(self) -> None:
		if not self.id:
			raise _exceptions.AppPathException("Can't update app path not defined by ID")
		urls = AppPath._urls_list
		for idx, url in enumerate(urls):
			if url["id"] == self.id:
				urls[idx] = self.dict()
				os.environ[_conf.ENV_URLS_PATH] = json.dumps(urls)
				return
		raise _exceptions.AppPathException(f"App path with ID {self.id} isn't exists")

	def __str__(self):
		return (f"<AppPath path='{self.path.replace(self.app, '')}' "
				f"authType={self.authType.name if self.authType else None} "
				f"method='{self.method}' app='{self.app}'>")

	def __repr__(self):
		return str(self)


class AppPathCreate(AppPath):
	def __init__(
		self,
		path: str,
		method: Literal["get", "put", "post", "delete"],
		app: enums.AppUrlEnum,
		authType: authEnum = None
	):
		super().__init__(path, authType, method, app)
		self.registered = self.__registered()
		self._register()

	def __registered(self) -> bool:
		for url in AppPath._urls_list:
			if url["method"] == self.method and url["path"] == self.path:
				return True
		return False

	def _register(self) -> Self:
		if self.registered:
			raise _exceptions.AppPathException(f"{self} already registered")
		if any(AppPath._urls_list):
			self.id = super()._urls_list[-1]["id"] + 1
		else:
			self.id = 1
		AppPath._urls_list.append(self.dict())
		os.environ[_conf.ENV_URLS_PATH] = json.dumps(AppPath._urls_list)
		self.registered = True
		return self

	def __add_expecting_data(
		self, title: Literal["cookies", "headers", "body", "query"], content: tuple[str]
	) -> Self:
		if any((not isinstance(val, str) for val in content)):
			raise _exceptions.AppPathException(f"Unsupportable {title} type - expected for 'str'")
		if title not in ["cookies", "headers", "body", "query"]:
			raise _exceptions.AppPathException(f"Unsupportable expecting content type '{title}'")
		if self._expects is None:
			self._expects = {}
		if title == "headers":
			content = map(lambda item: item.lower(), content)
		if title == "query":
			for param in content:
				if param != param.lower():
					warnings.warn(f"Note: '{param}' query param for app URL will be expected in lowercase")
		try:
			self._expects[title].extend(content)
		except KeyError:
			self._expects[title] = [*content]
		self._update()
		return self

	def cookies(self, *args) -> Self:
		args: tuple[str]
		return self.__add_expecting_data("cookies", args)

	def headers(self, *args) -> Self:
		args: tuple[str]
		return self.__add_expecting_data("headers", args)

	def body(self, *args) -> Self:
		args: tuple[str]
		return self.__add_expecting_data("body", args)

	def query(self, *args) -> Self:
		args: tuple[str]
		return self.__add_expecting_data("query", args)


class Server:
	"""
	Use like:
	server = Server("app", "app")
	server.include_router(app_router)	-->

	--> uvicorn start:server ...
	"""
	__app: FastAPI = None
	__global_router: APIRouter

	def __init__(
		self,
		title: str,
		prefix: enums.AppUrlEnum,
		openapi_tags: dict[str, str] = None,
		openapi_url: str = None,
		docs_url: str = None,
		redoc_url: str = None,
		description: str = None,
		summary: str = None,
		version: str = "0.0.1"
	):
		prefix = prefix.value
		self.title = title
		if config.GLOBAL_PREFIX in prefix:
			raise ValueError("Cannot use global prefix \"%s\" at app prefix" % config.GLOBAL_PREFIX)
		check_url_path(prefix)
		self.prefix = config.GLOBAL_PREFIX + prefix
		self.openapi_tags = openapi_tags
		if not openapi_url:
			self.openapi_url = self.prefix + "/openapi.json"
		if not docs_url:
			self.docs_url = self.prefix + "/docs"
		if not redoc_url:
			self.redoc_url = self.prefix + "/redoc"
		if not description or not summary:
			warnings.warn("Filled summary and description app fields are best practices.")
		self.description = description
		self.summary = summary
		self.version = version
		self.contact = config.CONTACT
		self.routers: list[APIRouter] = []

	def __str__(self):
		return f"<Server \"{self.title}\">"

	def __repr__(self):
		return str(self)

	def __call__(self, *args, **kwargs) -> FastAPI:
		print(Fore.BLUE + str(self) + Fore.RESET + f" Server URLs are available by prefix {self.prefix}")
		print(Fore.BLUE + str(self) + Fore.RESET + f" See the doc at {self.docs_url} & redoc at {self.redoc_url}.")
		if self.__app:
			return self.__app
		return self.__default_app()

	def __default_app(self) -> FastAPI:
		return FastAPI(
			title=self.title,
			openapi_tags=self.openapi_tags,
			openapi_url=self.openapi_url,
			docs_url=self.docs_url,
			redoc_url=self.redoc_url,
			description=self.description,
			summary=self.summary,
			version=self.version,
			contact=self.contact
		)

	def __define_app(self) -> None:
		if not self.openapi_tags:
			warnings.warn("It is recommended to use routers tags description when deploying app.")
		self.__global_router = APIRouter(prefix=self.prefix)
		self.__app = FastAPI(**self.__dict__, lifespan=self._lifespan)
		for router in self.routers:
			self.__global_router.include_router(router)
		self.__app.include_router(self.__global_router)

	def include_router(self, router) -> None:
		if isinstance(router, APIRouter):
			pkg_router = router
		else:
			try:
				pkg_router: APIRouter = getattr(router, "router")
			except AttributeError:
				raise AttributeError("App router must be the type of \"fastapi.APIRouter\" or "
									 "must contains \"router\" attribute (if it's a package of your application\"")
		if self.prefix == enums.AppUrlEnum.gateway.value:
			self.__check_router(pkg_router)
		self.routers.append(pkg_router)
		self.__define_app()

	@staticmethod
	def __check_router(router: APIRouter) -> None:
		for route in router.routes:
			router_data = route.__dict__
			path = router_data["path"]
			methods: set = router_data["methods"]
			if len(methods) > 1:
				raise _exceptions.RouterDefiningError(f"Router methods amount must be equal 1")
			method = next(iter(router_data["methods"]))
			# поставлю условие, что можно определить только один конкретный метод
			try:
				app = get_app_from_path(path)
			except _exceptions.AppNotFound as err:
				raise _exceptions.RouterDefiningError(str(err))
			registered_path = None
			for url in AppPath._urls_list:
				url_method, url_app, url_path = url["method"], url["app"], url["path"]
				if url_method == str(method).lower() and url_app == app.value and url_path == path:
					registered_path = url
			if not registered_path:
				raise _exceptions.NotRegisteredURL(f"<URL '{path}' '{method}' app='{app}'> isn't registered")

	@staticmethod
	@asynccontextmanager
	async def _lifespan(app: FastAPI):
		log(f"Starting {app.title}", app="common")  # noqa
		yield
		log(f"Shutting down {app.title}", app="common")  # noqa


class FastAPIResponse(JSONResponse):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.json = None  # для ошибок


def _convert_httpx_response_to_fastapi(response: Response) -> FastAPIResponse:
	json_ = response.json()
	result = FastAPIResponse(
		status_code=response.status_code,
		content=json_,
		headers=response.headers
	)
	result.json = json_
	# TODO если будет нужно добавлять куки, реализовать это здесь
	return result


async def app_request(url: str, app_path: AppPath, session: AsyncClient) -> FastAPIResponse:
	headers = dict(app_path.request.headers)
	json_ = await app_path.request.json()
	if json_:
		headers["content-length"] = str(len(json.dumps(json_)))  # нестыковка - хз почему. пересчитываю
	else:
		headers["content-length"] = "0"
	request_params = {
		"headers": headers,
		"cookies": app_path.request.cookies,
		"json": json_,
		"url": app_path.get_url(url)
	}
	try:
		match app_path.request.method.lower():
			case "get":
				response = await session.get(**request_params)
			case "post":
				response = await session.post(**request_params)
			case "put":
				response = await session.put(**request_params)
			case "delete":
				response = await session.delete(**request_params)
	except httpx.ConnectError:
		error_text = f"'{app_path.app.title().strip('/')}' server is not available"
		error(error_text, app="gateway")
		raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
							detail=error_text)
	return _convert_httpx_response_to_fastapi(response)
