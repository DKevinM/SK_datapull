// create map
document.addEventListener("DOMContentLoaded", function () {

// Bug: window.map was never a leftover Leaflet instance to guard against
// - browsers auto-expose any element with an id as window.<id>, so on
// every single page load window.map was actually the <div id="map">
// element itself. window.map.remove() (Element.prototype.remove(), not a
// Leaflet method) deleted the div from the page before Leaflet ever got
// to it, so L.map("map") below always threw "Map container not found."
// The map has never rendered in a real browser because of this. Only
// skip re-creating the map if window.map is an actual Leaflet instance.
if (window.map instanceof L.Map) {
    window.map.remove();
}

var map = L.map("map").setView([52.5,-106],6);
    
window.map = map;

var stationLayer = L.layerGroup().addTo(map);
    

L.tileLayer(
"https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
{
attribution: "© OpenStreetMap"
}).addTo(map);



// FireSmoke layers (GeoJSON polygons)

function smokeStyle(feature){

  var v = feature.properties.pm25;

  if(v < 5)  return {fillColor:"#ffffcc", weight:0, color:"none", fillOpacity:0.5};
  if(v < 10) return {fillColor:"#ffeda0", weight:0, color:"none", fillOpacity:0.6};
  if(v < 25) return {fillColor:"#feb24c", weight:0, color:"none", fillOpacity:0.7};
  if(v < 50) return {fillColor:"#f03b20", weight:0, color:"none", fillOpacity:0.8};
  if(v < 100) return {fillColor:"#bd0026", weight:0, color:"none", fillOpacity:0.9};

  return {fillColor:"#800026", weight:0, color:"none", fillOpacity:0.95};
}

function loadSmokeLayer(url){

  var layer = L.layerGroup();

  fetch(url + "?v=" + Date.now())
  .then(r => r.json())
  .then(data => {

    L.geoJSON(data,{
      style: smokeStyle,
      onEachFeature:function(feature,layer){

        var v = feature.properties.pm25;

        layer.bindTooltip(
          "Smoke PM2.5: " + v + " µg/m³"
        );

      }
    }).addTo(layer);

  });

  return layer;
}

// These five all pointed at 404s: "SK_Air_Map" isn't a real repo (this
// page's own data/ folder already has these files - same fix pattern as
// the AQHI lookup), and the PM2.5 layer pointed at lowercase "dkevinm"
// on a repo (AB_datapull) that doesn't have this file - the real one
// lives in this repo's own data/ folder too.
var smoke0 = loadSmokeLayer(
"data/firesmoke_now.geojson"
);
var smoke6 = loadSmokeLayer(
"data/firesmoke_6h.geojson"
);
var smoke12 = loadSmokeLayer(
"data/firesmoke_12h.geojson"
);
var smoke24 = loadSmokeLayer(
"data/firesmoke_24h.geojson"
);



var pm25Layer = loadPM25Layer(
  "data/SK_PM25_map.json"
);


// =====================================================
// AQHI GRID STYLE
// =====================================================

function aqhiGridStyle(feature){
  return {
    fillColor: feature.properties.color || "#808080",
    weight: 0,
    color: "none",
    fillOpacity: 0.45
  };
}
    
// =====================================================
// LOAD AQHI GRID
// =====================================================

function loadAQHIGrid(url){
  var layer = L.layerGroup();
  fetch(url + "?v=" + Date.now())
    .then(r => r.json())
    .then(data => {
      console.log("AQHI Grid loaded:", url);
      L.geoJSON(data, {
        style: aqhiGridStyle,
        onEachFeature: function(feature, layer){
          const aqhi =
            feature.properties.AQHI ??
            feature.properties.aqhi ??
            "N/A";
          const type =
            feature.properties.type ??
            "AQHI";
          layer.bindTooltip(
            type.toUpperCase() +
            "<br>AQHI: " + aqhi
          );
        }
      }).addTo(layer);
    })
    .catch(err => {
      console.error("AQHI grid failed:", url, err);
    });
  return layer;
}


// =====================================================
// AQHI GRID LAYERS
// =====================================================

var skCurrentGrid = loadAQHIGrid(
"https://raw.githubusercontent.com/DKevinM/SK_datapull/main/data/sk_current_blend.geojson"
);

var skForecastGrid = loadAQHIGrid(
"https://raw.githubusercontent.com/DKevinM/SK_datapull/main/data/sk_forecast_3h_blend.geojson"
);

var reginaCurrentGrid = loadAQHIGrid(
"https://raw.githubusercontent.com/DKevinM/SK_datapull/main/data/regina_current_blend.geojson"
);

var reginaForecastGrid = loadAQHIGrid(
"https://raw.githubusercontent.com/DKevinM/SK_datapull/main/data/regina_forecast_3h_blend.geojson"
);

    
var overlays = {

  "SK Current AQHI": skCurrentGrid,
  "SK Forecast AQHI": skForecastGrid,

  "Regina Current AQHI": reginaCurrentGrid,
  "Regina Forecast AQHI": reginaForecastGrid,

  "Smoke Now": smoke0,
  "Smoke +6 hr": smoke6,
  "Smoke +12 hr": smoke12,
  "Smoke +24 hr": smoke24,

  "PM2.5 Sensors": pm25Layer

};


L.control.layers(null, overlays, { position: "topright" }).addTo(map);




    

function getPMColor(pm){

  if(pm == null || isNaN(pm)) return "#808080";

  if (pm > 100) return "#640100";
  if (pm > 90)  return "#9a0100";
  if (pm > 80)  return "#cc0001";
  if (pm > 70)  return "#fe0002";
  if (pm > 60)  return "#fd6866";
  if (pm > 50)  return "#ff9835";
  if (pm > 40)  return "#ffcb00";
  if (pm > 30)  return "#fffe03";
  if (pm > 20)  return "#016797";
  if (pm > 10)  return "#0099cb";
  if (pm > 0)   return "#01cbff";

  return "#D3D3D3";
}

function pm25Style(feature){

  const v = feature.properties.pm25;

  return {
    fillColor: getPMColor(v),
    weight: 0.2,
    color: "#333",
    fillOpacity: 0.7
  };
}


function loadPM25Layer(url){

  var layer = L.layerGroup();

  fetch(url + "?v=" + Date.now())
  .then(r => r.json())
  .then(data => {

    console.log("PM25 features:", data.features?.length);

    L.geoJSON(data,{

      pointToLayer: function(feature, latlng){

        const p = feature.properties || {};
        const pm = p.pm25;

        return L.circleMarker(latlng, {
          radius: 6,
          fillColor: getPMColor(pm),
          color: "#333",
          weight: 1,
          fillOpacity: 0.8
        });

      },

      onEachFeature:function(feature,layer){

        const p = feature.properties || {};

        layer.bindPopup(`
          <b>${p.name ?? "Sensor"}</b><br>
          PM2.5: ${p.pm25 ?? "N/A"} µg/m³<br>
          Raw: ${p.pm_raw ?? "N/A"}<br>
          Humidity: ${p.humidity ?? "N/A"}%<br>
          Method: ${p.method ?? "N/A"}<br>
          Last Seen: ${p.last_seen ? new Date(p.last_seen).toLocaleString() : "N/A"}
        `);

      }

    }).addTo(layer);

  });

  return layer;
}


    

    
    
// Saskatchewan air monitoring API
var api =
"https://services3.arcgis.com/zcv98lgAl8xQ04cW/ArcGIS/rest/services/Hourly_Ambient_Air_Quality/FeatureServer/0/query?where=1=1&outFields=*&f=geojson";



    

// AQHI colors
function aqhiColor(v){

  if(v == null) return "#D3D3D3";

  if (v < 1)  return "#D3D3D3";
  if (v === 1) return "#01cbff";
  if (v === 2) return "#0099cb";
  if (v === 3) return "#016797";
  if (v === 4) return "#fffe03";
  if (v === 5) return "#ffcb00";
  if (v === 6) return "#ff9835";
  if (v === 7) return "#fd6866";
  if (v === 8) return "#fe0002";
  if (v === 9) return "#cc0001";
  if (v === 10) return "#9a0100";
  return "#640100";
}
    

function round1(v){
  if (v == null) return "N/A";
  return Number(v).toFixed(1);
}

// AQHI "DNA" driver chart - decomposes the real AQHI formula into each
// pollutant's share of the total, same as LiveMap's WCAS.html/ACA.html/
// index.html (search those for computeAQHIDrivers):
//   AQHI = (1000/10.4) * [exp(0.000537*O3) + exp(0.000871*NO2)
//                          + exp(0.000487*PM2.5) - 3]
function computeAQHIDrivers(no2, o3, pm25) {
  const tNO2 = Math.exp(0.000871 * no2) - 1;
  const tO3 = Math.exp(0.000537 * o3) - 1;
  const tPM25 = Math.exp(0.000487 * pm25) - 1;
  const total = tNO2 + tO3 + tPM25;
  if (!isFinite(total) || total <= 0) return null;
  return {
    NO2: 100 * tNO2 / total,
    O3: 100 * tO3 / total,
    "PM2.5": 100 * tPM25 / total
  };
}

function renderSKDNAChart(containerId, no2, o3, pm25, aqhiVal) {
  const container = document.getElementById(containerId);
  if (!container) return;
  if (no2 == null || o3 == null || pm25 == null) return; // station doesn't report all three - skip silently

  const shares = computeAQHIDrivers(Number(no2), Number(o3), Number(pm25));
  if (!shares) return;

  const label = document.createElement("div");
  label.style.fontWeight = "600";
  label.style.fontSize = "12px";
  label.style.marginBottom = "4px";
  label.textContent = "What's driving this AQHI";
  container.appendChild(label);

  const plotDiv = document.createElement("div");
  plotDiv.style.margin = "0 auto";
  container.appendChild(plotDiv);

  const theta = ["NO2", "O3", "PM2.5", "NO2"];
  const r = [shares.NO2, shares.O3, shares["PM2.5"], shares.NO2];
  const aqhiLabel = (aqhiVal != null && isFinite(aqhiVal)) ? (aqhiVal > 10 ? "10+" : Math.round(aqhiVal)) : "—";

  Plotly.newPlot(plotDiv, [{
    type: "scatterpolar",
    r: r,
    theta: theta,
    fill: "toself",
    hovertemplate: "%{theta}: %{r:.0f}%<extra></extra>"
  }], {
    title: { text: `AQHI: ${aqhiLabel}`, font: { size: 11 } },
    polar: {
      radialaxis: { visible: true, range: [0, 100], tickvals: [0, 50, 100], ticktext: ["0%", "50%", "100%"] }
    },
    showlegend: false,
    margin: { t: 24, b: 22, l: 30, r: 30 },
    height: 175,
    width: 200
  }, { displayModeBar: false });
}



   

    
    
var aqhiLookup = {};
// data/sk_aqhi_current.geojson doesn't exist in this repo (404) - was
// presumably renamed/replaced at some point and this fetch never got
// updated. loadStations() used to be called only inside this fetch's
// .then(), so the 404 silently prevented the ENTIRE station layer
// (markers, popups, the AQHI DNA chart) from ever loading, not just the
// AQHI lookup. Station markers/popups don't strictly need this lookup -
// they fall back to "N/A" for the 3hr AQHI display when it's missing -
// so loadStations() now always runs; this fetch is best-effort on top.
fetch("data/sk_aqhi_current.geojson")
.then(r => r.json())
.then(data => {
  data.features.forEach(f => {
    var p = f.properties;

    aqhiLookup[p.station.toUpperCase()] = {
      aqhi: Number(p.AQHI),
      time: p.updated
    };

  });

  console.log("AQHI lookup table:", aqhiLookup);
})
.catch(err => {
  console.warn("AQHI lookup unavailable (data/sk_aqhi_current.geojson):", err);
})
.finally(() => {
  loadStations();
});

    


    
// load stations
function loadStations(){
stationLayer.clearLayers();
fetch(api)
.then(r => r.json())
.then(data => {

  console.log("Stations returned:", data.features.length);

  var clean = data.features.filter(f => {

      if (!f.geometry) return false;
      if (!f.geometry.coordinates) return false;

      const lon = f.geometry.coordinates[0];
      const lat = f.geometry.coordinates[1];

      if (lon === null || lat === null) return false;
      if (isNaN(lon) || isNaN(lat)) return false;

      return true;

  });

  L.geoJSON(clean, {

    pointToLayer: function(feature,latlng){

      var p = feature.properties;

      var aqhiData = aqhiLookup[p.COMMUNITY.toUpperCase()];

      var aqhi = aqhiData ? aqhiData.aqhi : null;
      var aqhiTime = aqhiData ? new Date(aqhiData.time).toLocaleString() : "N/A";

      var color = aqhiColor(aqhi);
        
      var icon = L.divIcon({
        className: "aqhi-marker",
        html:
          "<div style='background:"+color+"'>"+
          (aqhi ?? "")+
          "</div>",
        iconSize: [38,38]
      });
        
      console.log(p.COMMUNITY, aqhiLookup[p.COMMUNITY.toUpperCase()]);  
      return L.marker(latlng,{icon:icon});

    },

    onEachFeature:function(feature,layer){

      var p = feature.properties;

      var aqhiData = aqhiLookup[p.COMMUNITY.toUpperCase()];

      var aqhi = aqhiData ? aqhiData.aqhi : null;
      var aqhiTime = aqhiData ? new Date(aqhiData.time).toLocaleString() : "N/A";

      var time = new Date(p.DATETIME).toLocaleString();

      var dnaContainerId = "dna-" + p.COMMUNITY.replace(/[^a-zA-Z0-9]/g, "");

      layer.bindPopup(
        "<b>"+p.COMMUNITY+"</b><br>"+
        "AQHI (3hr): "+(aqhi ?? "N/A")+"<br>"+
        "<hr>"+
        "PM2.5: "+round1(p.PM2_5)+" µg/m³<br>"+
        "NO₂: "+round1(p.NO2)+" ppb<br>"+
        "O₃: "+round1(p.O3)+" ppb<br>"+
        "Wind: "+round1(p.WS)+" km/h<br>"+
        "Temp: "+round1(p.TEMP)+" °C<br>"+
        "<hr>"+
        "Station time: "+time+"<br>"+
        "AQHI updated: "+aqhiTime+
        "<div id='"+dnaContainerId+"'></div>"
      );

      // AQHI "DNA" driver chart - same formula/pattern as LiveMap's
      // WCAS.html/ACA.html/index.html (see computeAQHIDrivers there),
      // adapted for SK's flat p.NO2/p.O3/p.PM2_5 properties instead of
      // AB's ParameterName-keyed rows array. Renders on popupopen since
      // the placeholder div isn't in the DOM until Leaflet opens the popup.
      layer.on("popupopen", function () {
        renderSKDNAChart(dnaContainerId, p.NO2, p.O3, p.PM2_5, aqhi);
      });

    }

  }).addTo(stationLayer);


});
}
});
