"use client";

import { useEffect, useRef } from "react";
import maplibregl from "maplibre-gl";
import { Protocol } from "pmtiles";
import "maplibre-gl/dist/maplibre-gl.css";
import { ARTIFACT_BASE } from "@/lib/duck";
import { RAMP } from "@/lib/format";

/**
 * MapLibre reading parcels.pmtiles over HTTP range requests.
 *
 * Browsers do not cache ranges against a single .pmtiles the way they cache
 * individual tile files, so tiles refetch on reload. That is expected; there is
 * no workaround here that is not worse than the problem.
 */

const SRC = "parcels";
const VISIBLE = "parcel-fill";

export default function ParcelMap({
  visibleIds,
  scores,
  hovered,
  onHover,
  selected,
  onSelect,
  flyTo,
}: {
  visibleIds: Set<string>;
  scores: Map<string, number>;
  hovered: string | null;
  onHover: (id: string | null) => void;
  selected: string | null;
  onSelect: (id: string | null) => void;
  flyTo: { lon: number; lat: number } | null;
}) {
  const host = useRef<HTMLDivElement>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const loaded = useRef(false);

  // The map is built once, so its handlers would otherwise close over the
  // first render's props - including the empty loc_id lookup, which silently
  // broke map-to-table hover.
  const cb = useRef({ onHover, onSelect });
  cb.current = { onHover, onSelect };

  useEffect(() => {
    if (!host.current || map.current) return;
    const protocol = new Protocol();
    maplibregl.addProtocol("pmtiles", protocol.tile);

    const m = new maplibregl.Map({
      container: host.current,
      // Dark, desaturated, no API key. The data is the only saturated colour.
      style: "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
      center: [-71.23, 42.33],
      zoom: 11.5,
      attributionControl: { compact: true },
    });
    m.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.current = m;

    m.on("load", () => {
      m.addSource(SRC, {
        type: "vector",
        url: `pmtiles://${ARTIFACT_BASE}/parcels.pmtiles`,
        promoteId: "loc_id",
      });

      const stops: (string | number)[] = [];
      RAMP.forEach((c, i) => stops.push(i / (RAMP.length - 1), c));

      m.addLayer({
        id: VISIBLE,
        type: "fill",
        source: SRC,
        "source-layer": SRC,
        paint: {
          "fill-color": ["interpolate", ["linear"], ["coalesce", ["get", "score"], 0], ...stops],
          "fill-opacity": [
            "case",
            ["boolean", ["feature-state", "filtered"], false],
            ["case", ["boolean", ["feature-state", "hover"], false], 0.95, 0.62],
            0.05,
          ],
        },
      });
      m.addLayer({
        id: "parcel-line",
        type: "line",
        source: SRC,
        "source-layer": SRC,
        paint: {
          "line-color": "#e8b24c",
          "line-width": [
            "case",
            ["boolean", ["feature-state", "selected"], false],
            2,
            ["boolean", ["feature-state", "hover"], false],
            1,
            0,
          ],
        },
      });

      let last: string | null = null;
      m.on("mousemove", VISIBLE, (e) => {
        const f = e.features?.[0];
        const id = f?.id != null ? String(f.id) : null;
        if (id !== last) {
          last = id;
          cb.current.onHover(id);
        }
        m.getCanvas().style.cursor = id ? "pointer" : "";
      });
      m.on("mouseleave", VISIBLE, () => {
        last = null;
        cb.current.onHover(null);
        m.getCanvas().style.cursor = "";
      });
      m.on("click", VISIBLE, (e) => {
        const f = e.features?.[0];
        if (f?.id != null) cb.current.onSelect(String(f.id));
      });
      loaded.current = true;
      m.triggerRepaint();
    });

    return () => {
      m.remove();
      map.current = null;
      loaded.current = false;
      maplibregl.removeProtocol("pmtiles");
    };
  }, []);

  // Tiles carry only loc_id and score; which parcels survive the filters is
  // known solely in DuckDB, so it is pushed in as feature state.
  useEffect(() => {
    const m = map.current;
    if (!m) return;
    const apply = () => {
      if (!m.getSource(SRC) || !m.isSourceLoaded(SRC)) return;
      for (const f of m.querySourceFeatures(SRC, { sourceLayer: SRC })) {
        if (f.id == null) continue;
        const id = String(f.id);
        m.setFeatureState(
          { source: SRC, sourceLayer: SRC, id },
          { filtered: visibleIds.has(id) },
        );
      }
    };
    apply();
    m.on("sourcedata", apply);
    m.on("moveend", apply);
    return () => {
      m.off("sourcedata", apply);
      m.off("moveend", apply);
    };
  }, [visibleIds, scores]);

  useFeatureFlag(map, "hover", hovered);
  useFeatureFlag(map, "selected", selected);

  useEffect(() => {
    if (flyTo && map.current)
      map.current.easeTo({ center: [flyTo.lon, flyTo.lat], zoom: Math.max(map.current.getZoom(), 16) });
  }, [flyTo]);

  return <div ref={host} className="h-full w-full" />;
}

/** Toggle one boolean feature-state key, clearing whatever held it before. */
function useFeatureFlag(
  map: React.RefObject<maplibregl.Map | null>,
  key: string,
  id: string | null,
) {
  const prev = useRef<string | null>(null);
  useEffect(() => {
    const m = map.current;
    if (!m || !m.getSource(SRC)) return;
    const set = (target: string | null, on: boolean) =>
      target &&
      m.setFeatureState({ source: SRC, sourceLayer: SRC, id: target }, { [key]: on });
    if (prev.current && prev.current !== id) set(prev.current, false);
    set(id, true);
    prev.current = id;
  }, [id, key, map]);
}
