# Library Site Deploy Dashboard

Small dashboard to deploy Library Site to the Pi and monitor status in real time.

## Setup

```bash
cd deploy-dashboard
npm install
```

## Run

```bash
npm start
```

Then open **http://localhost:3999** in your browser.

## Usage

- Click **Deploy** to start a deployment
- Watch live output in the log area
- Status badge shows: Idle → Running → Success/Failed
- Only one deploy can run at a time

## Port

Default port is 3999. Override with:

```bash
PORT=4000 npm start
```
