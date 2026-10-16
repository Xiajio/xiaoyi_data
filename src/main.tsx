import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { CatalogApp } from './catalog/CatalogApp';
import { CatalogApi, resolveCatalogBase } from './catalog/api';
import './styles.css';

// Production uses the same-origin proxy; Vite development uses the local service.
const defaultApi = import.meta.env.VITE_CATALOG_API || (import.meta.env.PROD ? window.location.origin : undefined);
const catalogBase = resolveCatalogBase(window.location.search, defaultApi);
const root = createRoot(document.getElementById('root')!);
if (catalogBase) {
  root.render(<StrictMode><CatalogApp api={new CatalogApi(catalogBase)} /></StrictMode>);
} else {
  // Load the independent demo before rendering, without a timed Suspense fallback.
  // This also keeps its simulation clock independent from application startup.
  void import('./App').then(({ App }) => root.render(<StrictMode><App /></StrictMode>));
}
