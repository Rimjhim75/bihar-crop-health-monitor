# Crop Health Monitor - Bihar
# NDVI from Sentinel-2 using Google Earth Engine, shown in a Streamlit web app

import datetime
import json
import ee
import folium
import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt
from streamlit_folium import st_folium

st.set_page_config(page_title="Crop Health Monitor - Bihar", layout="wide")


# Step 1: Start Earth Engine
# Online (Streamlit Cloud): uses the service account key saved in Streamlit Secrets
# On your laptop: uses the login you already did in Jupyter
@st.cache_resource
def start_ee():
    project = 'savvy-pad-470110-t2'

    try:
        key_text = st.secrets['EE_KEY']
    except Exception:
        key_text = None

    if key_text:
        info = json.loads(key_text)
        credentials = ee.ServiceAccountCredentials(info['client_email'], key_data=key_text)
        ee.Initialize(credentials=credentials, project=project)
    else:
        ee.Initialize(project=project)
    return True

start_ee()


# Step 2: Get Bihar districts (FAO GAUL level 2)
def get_bihar():
    districts = ee.FeatureCollection('FAO/GAUL/2015/level2')
    india = districts.filter(ee.Filter.eq('ADM0_NAME', 'India'))
    return india.filter(ee.Filter.eq('ADM1_NAME', 'Bihar'))


@st.cache_data
def get_district_names():
    bihar = get_bihar()
    return sorted(bihar.aggregate_array('ADM2_NAME').getInfo())


# Step 3: Cloud masking and NDVI functions
def maskS2sr(image):
    scl = image.select('SCL')
    # remove cloud shadow (3), clouds (8, 9), cirrus (10) and snow (11)
    mask = scl.neq(3).And(scl.neq(8)).And(scl.neq(9)).And(scl.neq(10)).And(scl.neq(11))
    masked = image.updateMask(mask).divide(10000)
    return masked.copyProperties(image, ['system:time_start'])


def add_ndvi(image):
    ndvi = image.normalizedDifference(['B8', 'B4']).rename('NDVI')
    return image.addBands(ndvi)


# Step 4: Main analysis for one district and date range
def run_analysis(district, start_date, end_date, cloud_limit):
    bihar = get_bihar()
    area = bihar.filter(ee.Filter.eq('ADM2_NAME', district))
    roi = area.geometry()

    s2 = (ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
          .filterDate(start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d'))
          .filterBounds(roi)
          .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', cloud_limit))
          .map(maskS2sr)
          .map(add_ndvi))

    n_images = s2.size().getInfo()
    if n_images == 0:
        return None

    # Mean NDVI map
    ndvi_mean = s2.select('NDVI').mean().clip(roi)
    vis = {'min': 0, 'max': 0.8, 'palette': ['brown', 'yellow', 'lightgreen', 'darkgreen']}
    tile_url = ndvi_mean.getMapId(vis)['tile_fetcher'].url_format

    # District boundary and centre point for the map
    boundary = roi.simplify(200).getInfo()
    centre = roi.centroid(1).coordinates().getInfo()   # [lon, lat]

    # Monthly mean NDVI
    n_months = (end_date.year - start_date.year) * 12 + (end_date.month - start_date.month) + 1
    first_day = ee.Date(start_date.strftime('%Y-%m-01'))
    months = ee.List.sequence(0, n_months - 1)

    def monthly_ndvi(n):
        n = ee.Number(n)
        s = first_day.advance(n, 'month')
        e = s.advance(1, 'month')
        col = s2.filterDate(s, e)

        # If the month has no images, use an empty (masked) image
        empty = ee.Image.constant(0).rename('NDVI').updateMask(ee.Image.constant(0))
        img = ee.Image(ee.Algorithms.If(col.size().gt(0), col.select('NDVI').mean(), empty))

        mean = img.reduceRegion(reducer=ee.Reducer.mean(), geometry=roi, scale=100,
                                maxPixels=1e9, bestEffort=True)
        return ee.Feature(None, {'month': s.format('YYYY-MM'), 'NDVI': mean.get('NDVI'), 'images': col.size()})

    monthly_fc = ee.FeatureCollection(months.map(monthly_ndvi))
    rows = [f['properties'] for f in monthly_fc.getInfo()['features']]
    df = pd.DataFrame(rows)
    df['NDVI'] = pd.to_numeric(df['NDVI'])

    return {'district': district, 'n_images': n_images, 'tile_url': tile_url,
            'boundary': boundary, 'centre': centre, 'df': df}


# Step 5: Page layout
st.title("Crop Health Monitor - Bihar")
st.write("Vegetation health (NDVI) from Sentinel-2 satellite images, for any district of Bihar.")

names = get_district_names()
default_index = names.index('Gaya') if 'Gaya' in names else 0

st.sidebar.header("Choose area and dates")
district = st.sidebar.selectbox("District", names, index=default_index)
start_date = st.sidebar.date_input("Start date", datetime.date(2025, 10, 1))
end_date = st.sidebar.date_input("End date", datetime.date(2026, 9, 30))
cloud_limit = st.sidebar.slider("Max cloud % per image", 10, 90, 80)
run = st.sidebar.button("Run analysis")

if run:
    n_months = (end_date.year - start_date.year) * 12 + (end_date.month - start_date.month) + 1
    if end_date <= start_date:
        st.warning("End date must be after start date.")
    elif n_months > 24:
        st.warning("Please choose a period of 24 months or less.")
    else:
        with st.spinner("Getting data from Google Earth Engine..."):
            st.session_state['result'] = run_analysis(district, start_date, end_date, cloud_limit)
            st.session_state['empty'] = st.session_state['result'] is None

# Step 6: Show the results
if st.session_state.get('empty'):
    st.error("No clear satellite images found for this district and period. Try other dates or a higher cloud limit.")
elif 'result' not in st.session_state:
    st.info("Choose a district and dates in the left panel, then click 'Run analysis'.")
else:
    res = st.session_state['result']
    df = res['df']

    st.subheader(res['district'] + " district")
    st.caption("Images used: " + str(res['n_images']))

    col1, col2 = st.columns(2)

    # NDVI map
    with col1:
        st.write("Mean NDVI map (brown = low, dark green = high)")
        m = folium.Map(location=[res['centre'][1], res['centre'][0]], zoom_start=9)
        folium.TileLayer(tiles=res['tile_url'], attr='Google Earth Engine', name='Mean NDVI', overlay=True).add_to(m)
        folium.GeoJson(res['boundary'], style_function=lambda x: {'color': 'black', 'fillOpacity': 0, 'weight': 2}).add_to(m)
        st_folium(m, height=450, width=600, returned_objects=[])

    # Time series chart
    with col2:
        st.write("Monthly mean NDVI")
        fig, ax = plt.subplots(figsize=(6, 4.5))
        ax.plot(df['month'], df['NDVI'], marker='o', color='green')
        ax.set_xlabel('Month')
        ax.set_ylabel('Mean NDVI')
        ax.grid(True)
        plt.xticks(rotation=45)
        st.pyplot(fig)

    # Simple statistics
    valid = df.dropna(subset=['NDVI'])
    if len(valid) > 0:
        c1, c2, c3 = st.columns(3)
        c1.metric("Mean NDVI", round(valid['NDVI'].mean(), 3))
        c2.metric("Highest NDVI", round(valid['NDVI'].max(), 3), valid.loc[valid['NDVI'].idxmax(), 'month'], delta_color="off")
        c3.metric("Lowest NDVI", round(valid['NDVI'].min(), 3), valid.loc[valid['NDVI'].idxmin(), 'month'], delta_color="off")

    with st.expander("Monthly values and image count"):
        st.dataframe(df)
