import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { CatalogApp } from './catalog/CatalogApp';
import { CatalogApi, resolveCatalogBase } from './catalog/api';
import './styles.css';

const catalogBase = resolveCatalogBase(window.location.search, import.meta.env.VITE_CATALOG_API);
createRoot(document.getElementById('root')!).render(
  <StrictMode>{catalogBase ? <CatalogApp api={new CatalogApi(catalogBase)} /> : <App />}</StrictMode>,
);
