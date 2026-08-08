const runtime = globalThis.__ANIME_JOURNAL_REACT_RUNTIME__?.ReactRouterDOM;
if (!runtime) throw new Error("Anime Journal router runtime is not ready");
export const {
  Await, BrowserRouter, Form, HashRouter, Link, MemoryRouter, NavLink, Navigate,
  Outlet, Route, Router, RouterProvider, Routes, ScrollRestoration, useActionData,
  useAsyncError, useAsyncValue, useBeforeUnload, useFetcher, useFetchers, useFormAction,
  useHref, useInRouterContext, useLinkClickHandler, useLoaderData, useLocation,
  useMatch, useMatches, useNavigate, useNavigation, useNavigationType, useOutlet,
  useOutletContext, useParams, useResolvedPath, useRevalidator, useRouteError,
  useRouteLoaderData, useRoutes, useSearchParams, useSubmit, useViewTransitionState,
  createBrowserRouter, createHashRouter, createMemoryRouter, defer, generatePath,
  isRouteErrorResponse, json, matchPath, matchRoutes, redirect, redirectDocument,
  resolvePath,
} = runtime;
export default runtime;
