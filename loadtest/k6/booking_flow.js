// The peak hour of the platform: clients looking for a slot, opening the card
// of a master, and some of them booking.
//
//   80 % GET  /api/v1/availability          a random master, service and day
//   15 % GET  /api/v1/masters/{id}/card     a random master
//    5 % POST /api/v1/bookings              a random free slot
//
// Ramped to 50 requests a second over two minutes and held for five. The SLOs
// are thresholds, so a run that misses one exits non-zero:
//
//   errors < 1 %, availability p95 < 1 s, booking creation p95 < 500 ms.
//
// Every request is made by one of the clients of loadtest/k6/users.json
// (scripts/loadtest_users.py): the gateway lets an anonymous address make 30
// requests a minute and a user 120, and all of k6 is one address. The file
// holds an access token per client, signed in right before the run; a token
// lives 15 minutes, which is the longest a run can last.
//
// Usage: make loadtest (README.md next to this file).

import { check, fail } from "k6";
import exec from "k6/execution";
import http from "k6/http";
import { Counter, Rate, Trend } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://barber.local";
const RATE = Number(__ENV.RATE || 50);
const RAMP = __ENV.RAMP || "2m";
const HOLD = __ENV.HOLD || "5m";
// Days ahead a client looks at and books in: from tomorrow, so that no slot is
// inside the lead time of the salon, to the end of the seeded schedules.
const DAYS_AHEAD = Number(__ENV.DAYS_AHEAD || 13);

const USERS = JSON.parse(open("./users.json"));

// The share of each operation: three scenarios of the same shape, so that each
// keeps its own rate and its own tag whatever the others do.
const MIX = { availability: 0.8, card: 0.15, booking: 0.05 };

function profile(share) {
  const rate = Math.max(1, Math.round(RATE * share * 10));
  return {
    executor: "ramping-arrival-rate",
    // Rates in tenths of a request a second: 5 % of 50 RPS is 2.5.
    timeUnit: "10s",
    startRate: 0,
    preAllocatedVUs: Math.max(5, Math.ceil(rate / 2)),
    maxVUs: Math.max(20, rate * 4),
    stages: [
      { target: rate, duration: RAMP },
      { target: rate, duration: HOLD },
    ],
  };
}

export const options = {
  scenarios: {
    availability: { ...profile(MIX.availability), exec: "availability" },
    card: { ...profile(MIX.card), exec: "card" },
    booking: { ...profile(MIX.booking), exec: "booking" },
  },
  thresholds: {
    // "Errors" are what a client could not get past: a 5xx, a timeout, an
    // unexpected 4xx. A slot taken by someone else in between is the
    // contract working, not an error (see slot_conflicts).
    http_req_failed: ["rate<0.01"],
    "http_req_duration{op:availability}": ["p(95)<1000"],
    // The look before a booking is the same query, under the same SLO.
    "http_req_duration{op:booking_lookup}": ["p(95)<1000"],
    "http_req_duration{op:create_booking}": ["p(95)<500"],
    // No SLO of its own; recorded so the report shows it.
    "http_req_duration{op:card}": ["p(95)<2000"],
    // A run where most bookings collide measures conflicts, not bookings.
    slot_conflicts: ["rate<0.2"],
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
};

const slotConflicts = new Rate("slot_conflicts");
const bookingsCreated = new Counter("bookings_created");
const slotsOffered = new Trend("slots_offered");

// --- setup: sign in the clients, learn the catalog ----------------------------

function json(response, what) {
  if (response.status !== 200) {
    fail(`${what}: ${response.status} ${response.body}`);
  }
  return response.json();
}

function pages(path, headers) {
  const items = [];
  let cursor = null;
  do {
    const query = cursor ? `limit=100&cursor=${encodeURIComponent(cursor)}` : "limit=100";
    const page = json(http.get(`${BASE_URL}${path}?${query}`, { headers }), `GET ${path}`);
    items.push(...page.items);
    cursor = page.next_cursor;
  } while (cursor);
  return items;
}

export function setup() {
  const tokens = USERS.users.map((user) => user.access_token);
  const headers = { Authorization: `Bearer ${tokens[0]}` };

  // Every active master with every service they offer: the pairs a client can
  // ask availability for and book.
  const pairs = [];
  const masters = [];
  for (const salon of pages("/api/v1/salons", headers)) {
    for (const master of pages(`/api/v1/salons/${salon.id}/masters`, headers)) {
      if (!master.is_active) continue;
      masters.push(master.id);
      const profile = json(
        http.get(`${BASE_URL}/api/v1/masters/${master.id}`, { headers, tags: { op: "setup" } }),
        `master ${master.id}`,
      );
      for (const service of profile.services) {
        pairs.push({ master: master.id, service: service.service_id });
      }
    }
  }
  if (pairs.length === 0) fail("the catalog is empty: was the platform seeded?");
  console.log(`${tokens.length} clients, ${masters.length} masters, ${pairs.length} pairs`);
  return { tokens, masters, pairs };
}

// --- the three operations -------------------------------------------------------

function pick(items) {
  return items[Math.floor(Math.random() * items.length)];
}

function client(data) {
  // Spread the clients over the iterations, so that each stays well below the
  // per-user limits of the gateway.
  const token = data.tokens[exec.scenario.iterationInTest % data.tokens.length];
  return { Authorization: `Bearer ${token}` };
}

function dayAhead() {
  const day = new Date(Date.now() + (1 + Math.floor(Math.random() * DAYS_AHEAD)) * 86400000);
  return day.toISOString().slice(0, 10);
}

function readAvailability(data, pair, op) {
  const date = dayAhead();
  return http.get(
    `${BASE_URL}/api/v1/availability?master_id=${pair.master}&service_id=${pair.service}` +
      `&date_from=${date}`,
    { headers: client(data), tags: { op, name: "GET /api/v1/availability" } },
  );
}

export function availability(data) {
  const response = readAvailability(data, pick(data.pairs), "availability");
  check(response, { "availability 200": (r) => r.status === 200 });
}

export function card(data) {
  const master = pick(data.masters);
  const response = http.get(`${BASE_URL}/api/v1/masters/${master}/card`, {
    headers: client(data),
    tags: { op: "card", name: "GET /api/v1/masters/{id}/card" },
  });
  check(response, {
    "card 200": (r) => r.status === 200,
    "card has slots": (r) => r.status === 200 && r.json().slots_status === "ok",
  });
}

// The way a client books: look at a day, take one of its free starts. Not the
// first one -- every client would race for the same start, and the run would
// measure conflicts. A day with nothing free is a look, not a booking.
export function booking(data) {
  const pair = pick(data.pairs);
  const looked = readAvailability(data, pair, "booking_lookup");
  if (!check(looked, { "lookup 200": (r) => r.status === 200 })) return;
  const slots = looked.json().days.flatMap((day) => day.slots);
  slotsOffered.add(slots.length);
  if (slots.length === 0) return;

  const headers = {
    ...client(data),
    "Content-Type": "application/json",
    "Idempotency-Key": `k6-${exec.vu.idInTest}-${exec.scenario.iterationInTest}-${Date.now()}`,
  };
  const response = http.post(
    `${BASE_URL}/api/v1/bookings`,
    JSON.stringify({ master_id: pair.master, service_id: pair.service, start_at: pick(slots) }),
    {
      headers,
      tags: { op: "create_booking", name: "POST /api/v1/bookings" },
      responseCallback: http.expectedStatuses(201, 409),
    },
  );
  slotConflicts.add(response.status === 409);
  if (response.status === 201) bookingsCreated.add(1);
  check(response, { "booking 201 or 409": (r) => r.status === 201 || r.status === 409 });
}

export function handleSummary(data) {
  const out = __ENV.SUMMARY || "results/summary.json";
  return {
    stdout: textSummary(data),
    [out]: JSON.stringify(data, null, 2),
  };
}

// The few numbers the report needs, without the remote module of k6.
function textSummary(data) {
  const metric = (name) => data.metrics[name];
  const line = (label, name) => {
    const m = metric(name);
    if (!m) return `  ${label}: no data`;
    const v = m.values;
    return (
      `  ${label}: avg=${fmt(v.avg)} med=${fmt(v.med)} ` +
      `p95=${fmt(v["p(95)"])} p99=${fmt(v["p(99)"])} max=${fmt(v.max)}`
    );
  };
  const failed = metric("http_req_failed").values;
  const conflicts = metric("slot_conflicts");
  const created = metric("bookings_created");
  const thresholds = Object.entries(data.metrics)
    .filter(([, m]) => m.thresholds)
    .flatMap(([name, m]) =>
      Object.entries(m.thresholds).map(([t, r]) => `  ${r.ok ? "ok  " : "FAIL"} ${name} ${t}`),
    );
  return [
    "",
    "requests",
    line("availability   ", "http_req_duration{op:availability}"),
    line("card           ", "http_req_duration{op:card}"),
    line("booking lookup ", "http_req_duration{op:booking_lookup}"),
    line("create booking ", "http_req_duration{op:create_booking}"),
    `  total: ${metric("http_reqs").values.count} at ${metric("http_reqs").values.rate.toFixed(1)}/s`,
    `  failed: ${(failed.rate * 100).toFixed(3)} % (${failed.passes} of ${failed.passes + failed.fails})`,
    `  bookings created: ${created ? created.values.count : 0}, ` +
      `conflicts: ${conflicts ? (conflicts.values.rate * 100).toFixed(1) : 0} %`,
    "thresholds",
    ...thresholds,
    "",
  ].join("\n");
}

function fmt(ms) {
  return ms === undefined ? "-" : `${ms.toFixed(0)}ms`;
}
