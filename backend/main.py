from fastapi import FastAPI, Form, Request, HTTPException, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
import sqlite3
import stripe
import os
import json
import httpx
import time
import random
import asyncio

# run source ../.env to get path variables
from dotenv import load_dotenv
load_dotenv('../.env')


OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
stripe.api_key = os.environ["STRIPE_API_KEY"]
HOST = os.environ["HOST"]

app = FastAPI()



def setup_db(conn):
    with conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                machine_id TEXT UNIQUE, -- globally unique machine id
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                -- the user the message is associated with. linked to users.id via foreign key
                user_id INTEGER,

                -- the message, in openai format. if role == "user", then this is the user's message,
                -- if role == "assistant" then this is the assistant's response to a previous message.
                role TEXT,
                content TEXT,

                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                user_id INTEGER,

                app TEXT,
                window_title TEXT,
                time INTEGER NOT NULL, -- epoch time, from swift
                -- TODO add more fields for tracking activity

                FOREIGN KEY (user_id) REFERENCES users(id)
            );
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            user_id INTEGER,

            timesinks TEXT,
            endorsed_activities TEXT,

            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """)


@app.on_event("startup")
def startup():
    # setup db
    app.state.db = sqlite3.connect("db.sqlite3")
    setup_db(app.state.db)


@app.on_event("shutdown")
def shutdown():
    app.state.db.close()


@app.post("/create-checkout-session")
def create_checkout_session(lookup_key: str = Form(...)):
    try:
        print(lookup_key)
        prices = stripe.Price.list(
            lookup_keys=[lookup_key],
            expand=["data.product"]
        )
        print(prices)
        checkout_session = stripe.checkout.Session.create(
            line_items=[
                {
                    "price": prices.data[0].id,
                    "quantity": 1,
                }
            ],
            mode='subscription',
            success_url=f"{HOST}/success.html?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{HOST}/cancel.html",
        )
        return RedirectResponse(url=checkout_session.url, status_code=303)
    except Exception as e:
        print(e)
        raise HTTPException(status_code=500)



@app.post("/create-portal-session")
def create_portal_session(session_id: str = Form(...)):
    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id)
        portalSession = stripe.billing_portal.Session.create(
            customer=checkout_session.customer,
            return_url=HOST,
        )
        return RedirectResponse(url=portalSession.url, status_code=303)
    except Exception as e:
        print(e)
        raise HTTPException(status_code=500)



@app.post("/webhook")
async def webhook_received(request: Request):
    webhook_secret = 'whsec_12345'
    request_data = await request.json()
    body = await request.body()

    if webhook_secret:
        signature = request.headers.get('stripe-signature')
        if not signature:
            return HTTPException(status_code=400, detail="Missing stripe-signature header")

        try:
            event = stripe.Webhook.construct_event(
                payload=body, sig_header=signature, secret=webhook_secret)
            data = event['data']
        except Exception as e:
            return e

        event_type = event['type']
    else:
        data = request_data['data']
        event_type = request_data['type']

    print('stripe event ' + event_type)

    # Handle different event types here
    # FIXME: Not handling these is literally illegal (impossible to unsubscribe)
    raise HTTPException(status_code=500, detail="Server error: Not implemented")

    return {"status": "success"}



SYSTEM_PROMPT = """
You are a productivity assistant who only interrupts if a user is definitely distracted from their task (e.g. on social media). If they are definitely distracted, kindly try and motivate them to work. Otherwise, affirm on-task activity with "Great work!" and nothing else. Adapt when the user updates their preferences.

After the user specifies their goal, encourage them and tell them how often you'll be checking in on them, and ask if they want to change how frequently you check in.
""".strip()

INITIAL_MESSAGE = """
Hi there! what do you want to work on right now? I can help you stay on task and be more productive!
""".strip()


client = httpx.AsyncClient(headers={"Authorization": f"Bearer {OPENAI_API_KEY}"}, timeout=100)



# call the trigger function with "trigger": true or "trigger": false
TRIGGER_PROMPT = """
If the user is on a timesink, then trigger the app. Otherwise, pass false to do nothing.
The user's common time sinks are:
{timesinks}

Over the last {minutes} minutes the user's activity has been:
{activity}
""".strip()


# function to determine whether to trigger the app or not
async def should_trigger(prompt: str) -> bool:
    print('trigger prompt:\n', prompt)
    messages = [{'role': 'system', 'content': prompt}]
    resp = await client.post(
        "https://api.openai.com/v1/chat/completions",
        json={
            "model": "gpt-3.5-turbo",
            "messages": messages,
            "functions": [
                {
                    # this should be either True or False, always called.
                    "name": "trigger",
                    "description": "If true, trigger the app. If false, do nothing.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "trigger": {
                                "type": "boolean"
                            }
                        },
                        "required": ["trigger"],
                    },
                }
            ],
            "max_tokens": 100,
        }
    )
    resp.raise_for_status()
    resp_data = resp.json()
    message = resp_data['choices'][0]['message']
    # trigger iff the message is a function call to trigger
    trigger = False
    if message.get("function_call") and message["function_call"]["name"] == 'trigger':
        trigger = message["function_call"]["arguments"]["trigger"]
    print('trigger:', trigger)
    return trigger


class WebSocketHandler():
    """
    Web socket handler, one per connection. Handles
    * Keeping client, server, and database in sync
    * Querying GPT for triggers and sending messages when required
    """

    def __init__(self, ws, db):
        self.ws = ws
        self.db = db
        self.user_id = None


    def record_msg(self, msg):
        self.db.execute("INSERT INTO messages (user_id, content, role) VALUES (?, ?, ?)", (self.user_id, msg['content'], msg['role']))
        self.db.commit()

    def record_activity(self, data):
        c = self.db.cursor()
        c.execute("INSERT INTO activity (user_id, app, window_title, time) VALUES (?, ?, ?, ?)", (self.user_id, data['app'], data['window_title'], data['time']))
        self.db.commit()


    async def send_msg(self, msg):
        await self.ws.send_json(msg)


    async def send_and_record_msg(self, msg):
        self.record_msg(msg)
        await self.send_msg(msg)


    async def on_register(self, data):
        print(f"registering user {data}")
        user = data['user']
        # plop into database if not already there
        c = self.db.cursor()
        c.execute(
            "INSERT OR IGNORE INTO users (machine_id) VALUES (?)",
            (user['machine_id'],)
        )
        self.user_id, = c.execute("SELECT id FROM users WHERE machine_id = ?", (user['machine_id'],)).fetchone()
        assert self.user_id is not None

        # add empty settings row if it doesn't exist
        settings = None
        try:
            settings = await self.get_settings()
            # send settings to client
            await self.send_msg({"type": "settings", **settings})
        except KeyError:
            c.execute("INSERT INTO settings (user_id) VALUES (?)", (self.user_id,))
            self.db.commit()

        # fetch 100 most recent messages & shove into client
        c.execute("SELECT id, role, content FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT 100", (self.user_id,))
        # must send in reversed order because we want to send oldest first
        for _, role, content in reversed(c.fetchall()):
            await self.send_msg({"type": "msg", "role": role, "content": content})

        print(f"done registering {data} user id {self.user_id}")


    async def receive(self, timeout=10):
        try:
            text = await asyncio.wait_for(self.ws.receive_text(), timeout=timeout)
            return json.loads(text)
        except asyncio.TimeoutError:
            return None


    @staticmethod
    def add_activity_dur(activities):
        # prevent IndexErrors
        if len(activities) == 0:
            return activities
        # add time spent on each app by subtracting the time of the next app from the time of the current app
        for i in range(len(activities) - 1):
            activities[i]['dur'] = activities[i]['time'] - activities[i+1]['time']
        # add time spent on last app by subtracting the time of the last app from now
        activities[-1]['dur'] = time.time() - activities[-1]['time']
        return activities


    async def check_in(self, max_n=20, last_n_seconds=600):
        print('checking in...')
        # get the activites from user in the last 10 minutes (n secnods)
        now = time.time()
        c = self.db.cursor()
        rows = c.execute(
            f"SELECT app, window_title, time FROM activity WHERE user_id = ? AND time > ? ORDER BY time DESC LIMIT ?",
            (self.user_id, now - last_n_seconds, max_n)
        ).fetchall()
        if len(rows) == 0:
            # just put most recent activity
            rows = c.execute(f"SELECT app, window_title, time FROM activity WHERE user_id = ? ORDER BY time DESC LIMIT 1", (self.user_id,)).fetchall()
        activities = [{"app": app, "window_title": window_title, "time": time} for app, window_title, time in rows]

        self.add_activity_dur(activities)
        # filter things with < 10s of activity
        # activities = [a for a in activities if a['dur'] > 10]

        timesinks: str = (await self.get_settings()).get('timesinks') or ''
        if timesinks.strip() == '':
            print('No time sinks yet -- skipping check in')
            return

        activity = '\n'.join(f"{a['dur']:.0f} seconds on {a['app']} - {a['window_title']}" for a in activities)
        if timesinks.strip() == '':
            print('no timesinks recorded yet')
            return

        trigger = await should_trigger(TRIGGER_PROMPT.format(minutes=last_n_seconds//60, timesinks=timesinks, activity=activity))
        if trigger:
            # TODO: send GPT4 response
            await self.send_and_record_msg({"role": "assistant", "content": "Hey! You're on a timesink. You should get back to work."})


    async def update_settings(self, settings_msg):
        print('updating settings', settings_msg)
        # insert timesinks data into database. we don't delete anything.
        timesinks: str = settings_msg["timesinks"]
        endorsed_activities: str = settings_msg["endorsed_activities"]
        c = self.db.cursor()
        c.execute("INSERT INTO settings (user_id, timesinks, endorsed_activities) VALUES (?, ?, ?)", (self.user_id, timesinks, endorsed_activities))
        self.db.commit()


    async def get_settings(self):
        assert self.user_id is not None, "user must be registered before getting settings"
        # fetch most recent settings from that user id
        c = self.db.cursor()
        c.execute("SELECT timesinks, endorsed_activities FROM settings WHERE user_id = ? ORDER BY id DESC LIMIT 1", (self.user_id,))
        settings = c.fetchone()
        if not settings:
            raise KeyError(f"user {self.user_id} doesn't have settings yet")
        return {"timesinks": settings[0], "endorsed_activities": settings[1]}


    async def run(self):
        await self.ws.accept()

        # get registration
        data = await self.ws.receive_json()
        if data['type'] == 'register':
            await self.on_register(data)
        else:
            raise ValueError(f"message type {data['type']} disallowed for first message")

        check_in_interval = 300
        last_check_in = time.time()

        while True:
            data = await self.receive(timeout=10)
            if not data:
                if time.time() - last_check_in > check_in_interval:
                    await self.check_in(last_n_seconds=check_in_interval)

                continue

            if data['type'] == 'activity_info':
                self.record_activity(data)
            elif data['type'] == 'msg':
                self.record_msg(data)
                await self.send_and_record_msg({"type": "msg", "role": "assistant", "content": "TODO: respond"})
            elif data['type'] == 'settings':
                await self.update_settings(data)
            else:
                raise ValueError(f"Unknown message type: {data['type']}")



@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    handler = WebSocketHandler(websocket, app.state.db)
    await handler.run()


app.mount("/", StaticFiles(directory="static", html=True))
