"""Fleet simulator: N virtual cars sending battery + location to Ditto.

Each update carries:
  seq     - increasing counter per car (lets the probe detect lost updates)
  sentAt  - unix time the car sent it (lets the probe measure staleness)

Every second it writes state/sent.json with the last seq Ditto ACKNOWLEDGED
for each car. The probe compares that with what Ditto actually stores.

Run:  python simulator.py          (Ctrl+C to stop)
"""
import asyncio
import json
import os
import random
import time

import httpx

import config

stats = {"sent": 0, "ok": 0, "failed": 0}


class Car:
    def __init__(self, index: int):
        self.id = config.car_id(index)
        self.level = random.uniform(40, 100)
        self.lat = 12.97 + random.uniform(-0.1, 0.1)
        self.lon = 77.59 + random.uniform(-0.1, 0.1)
        self.seq = 0
        self.acked_seq = 0
        self.acked_at = 0.0

    def next_features(self) -> dict:
        self.seq += 1
        self.level = max(0.0, self.level - random.uniform(0.0, 0.3))
        self.lat += random.uniform(-0.0005, 0.0005)
        self.lon += random.uniform(-0.0005, 0.0005)
        return {
            "battery": {"properties": {"level": round(self.level, 1),
                                       "seq": self.seq,
                                       "sentAt": time.time()}},
            "location": {"properties": {"lat": round(self.lat, 6),
                                        "lon": round(self.lon, 6)}},
        }


async def create_twin(client: httpx.AsyncClient, car: Car, sem: asyncio.Semaphore) -> None:
    body = {"attributes": {"model": "EV-X", "fleet": "hackathon"},
            "features": car.next_features()}
    async with sem:
        r = await client.put(f"/api/2/things/{car.id}", json=body)
    if r.status_code not in (201, 204):
        raise RuntimeError(f"Creating {car.id} failed: HTTP {r.status_code} {r.text[:200]}")
    car.acked_seq, car.acked_at = car.seq, time.time()


async def drive(client: httpx.AsyncClient, car: Car) -> None:
    await asyncio.sleep(random.uniform(0, config.SEND_INTERVAL_S))  # spread load evenly
    while True:
        features = car.next_features()
        stats["sent"] += 1
        try:
            r = await client.put(f"/api/2/things/{car.id}/features", json=features)
            if r.status_code in (201, 204):
                car.acked_seq, car.acked_at = car.seq, time.time()
                stats["ok"] += 1
            else:
                stats["failed"] += 1
        except httpx.HTTPError:
            stats["failed"] += 1
        await asyncio.sleep(config.SEND_INTERVAL_S)


def write_state(cars: list[Car]) -> None:
    os.makedirs(config.STATE_DIR, exist_ok=True)
    data = {"written_at": time.time(),
            "stats": dict(stats),
            "cars": {c.id: {"acked_seq": c.acked_seq, "acked_at": c.acked_at} for c in cars}}
    tmp = config.SENT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, config.SENT_FILE)  # atomic: probe never reads a half-written file


async def reporter(cars: list[Car]) -> None:
    tick = 0
    while True:
        await asyncio.sleep(1)
        write_state(cars)
        tick += 1
        if tick % 10 == 0:
            print(f"[sim] sent={stats['sent']} ok={stats['ok']} failed={stats['failed']}", flush=True)


async def main() -> None:
    cars = [Car(i) for i in range(1, config.NUM_CARS + 1)]
    async with httpx.AsyncClient(base_url=config.DITTO_URL,
                                 auth=(config.DITTO_USER, config.DITTO_PASS),
                                 timeout=config.REQUEST_TIMEOUT_S,
                                 limits=httpx.Limits(max_connections=50)) as client:
        print(f"[sim] creating {len(cars)} twins at {config.DITTO_URL} ...", flush=True)
        sem = asyncio.Semaphore(10)
        await asyncio.gather(*(create_twin(client, c, sem) for c in cars))
        write_state(cars)
        print(f"[sim] driving: 1 update per car every {config.SEND_INTERVAL_S}s (Ctrl+C to stop)", flush=True)
        try:
            await asyncio.gather(reporter(cars), *(drive(client, c) for c in cars))
        finally:
            write_state(cars)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[sim] stopped")
