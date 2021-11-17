import statistics
import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCodes
import time
import datetime
import socket
import json
import argparse
import random
import threading
import os
from typing import Any, List, Dict
import yaml

N = 50
stats_fname = "qos-stats.txt"
hostname = "m.shohamc1.com"
port = 80
keepalive = 60


def periodic_disconnect(client: mqtt.Client, userdata: Dict[str, Any]):
    """Periodically disconnects the client based on the specified disconnect_perc. Ends on KeyboardInterrupt."""
    while not userdata["disconnect_event"].is_set():
        time.sleep(userdata["disconnect_interval"])
        n: float = random.uniform(0, 1)
        if n <= userdata["disconnect_perc"]:
            client.disconnect()
            userdata["data"].append(
                {
                    "seq_num": -1,
                    "last_seq_num": userdata["data"][-1]["seq_num"]
                    if len(userdata["data"]) > 0
                    else -1,
                    "disconnect_time": time.time_ns() // (10 ** 6),
                    "reconnect_time": -1,
                }
            )
            # wait for reconnect before starting next interval
            time.sleep(userdata["disconnect_duration"])


def on_connect(
    client: mqtt.Client,
    userdata: Dict[str, Any],
    flags: Dict[str, Any],
    reason: ReasonCodes,
    properties: Properties,
):
    """Callback for when client receives a CONNACK response from broker"""
    print("Connected with reason code " + reason.getName())

    # Subscribing in on_connect() means that if we lose the connection and
    # reconnect then subscriptions will be renewed.
    client.subscribe("test", qos=userdata["qos"])

    # Create and start disconnect thread only if:
    #   We want disconnections to happen (ie. disconnect_perc > 0)
    #   Thread has not already been created and started
    if userdata["disconnect_thread"] is None and userdata["disconnect_perc"] > 0:
        userdata["disconnect_event"] = threading.Event()
        userdata["disconnect_thread"] = threading.Thread(
            target=periodic_disconnect, args=[client, userdata]
        )
        userdata["disconnect_thread"].start()


def on_message(client: mqtt.Client, userdata: Dict[str, Any], msg: mqtt.MQTTMessage):
    """Callback for when a PUBLISH message is received from the server"""
    rcv_time: int = time.time_ns() // (10 ** 6)
    print(f"{msg.topic} {msg.payload} {msg.mid}")
    if msg.topic == "test":
        content: str = msg.payload.decode()
        seq_num, send_time = content.split(" ")
        pkt_data: Dict[str, Any] = {
            "seq_num": int(seq_num),
            "send_time": int(send_time),
            "rcv_time": rcv_time,
            "time_diff": (rcv_time - int(send_time)),
            "qos": msg.qos,
        }
        userdata["data"].append(pkt_data)


def on_log(client, userdata, level, buf):
    """Logs messages sent and received by client"""
    print(f"[{level}] {buf}")


if __name__ == "__main__":
    # Process arguments
    parser = argparse.ArgumentParser(
        prog="sub-client",
        usage="Usage: python sub-client.py -f <input-file-path>",
    )

    parser.add_argument(
        "-f",
        "--file",
        help="Path to file with input variables",
        required=False,
        default="",
    )
    args = parser.parse_args()

    # Initialise userdata to be passed to client callbacks
    userdata: Dict[str, Any] = {}
    data: List[Dict[str, Any]] = []
    userdata["qos"] = 0
    userdata["net_cond"] = "normal"
    userdata["total_packets"] = 50
    userdata["data"] = data
    # variables for periodic disconnect
    userdata["disconnect_perc"] = 0
    userdata["disconnect_interval"] = 10
    userdata["disconnect_duration"] = 10
    userdata["disconnect_event"] = None  # Optional[threading.Event]
    userdata["disconnect_thread"] = None  # Optional[threading.Thread]

    if args.file:
        if not os.path.exists(args.file):
            print(f"{args.file} is not a valid path. Using default values.")
        else:
            with open(args.file, "r") as input_f:
                input_values = yaml.safe_load(input_f)
                if input_values.get("subscriber", None) is not None:
                    userdata = {**userdata, **input_values["subscriber"]}
                if input_values.get("shared", None) is not None:
                    userdata = {**userdata, **input_values["shared"]}

    print(f"userdata: {userdata}")

    try:
        # Initialise client and callbacks
        client = mqtt.Client(
            client_id="test-sub",
            userdata=userdata,
            protocol=mqtt.MQTTv5,
            transport="websockets",
        )
        client.username_pw_set("test", "test")

        client.on_connect = on_connect
        client.on_message = on_message
        client.on_log = on_log

        # Initial connect
        connected = False
        properties = Properties(PacketTypes.CONNECT)
        properties.SessionExpiryInterval = 30
        while not connected:
            try:
                client.connect(
                    hostname,
                    port,
                    keepalive,
                    clean_start=False,
                    properties=properties,
                )
                connected = True
            except (socket.timeout, mqtt.WebsocketConnectionError):
                print("connection error, retrying...")

        start_time = datetime.datetime.now()
        # Loop forever with periodic disconnects and reconnects
        while True:
            client.loop_forever()
            # client disconnects and loop stops --> initiate reconnect after disconnect_duration
            time.sleep(userdata["disconnect_duration"])
            connected = False
            while not connected:
                try:
                    client.reconnect()
                    connected = True
                    userdata["data"][-1]["reconnect_time"] = time.time_ns() // (10 ** 6)
                except socket.timeout:
                    pass
    except KeyboardInterrupt:
        # Write collected data to file
        #   Can delete if we don't need to collect all the generated data
        #   Just collecting for now in case we want to do further analysis later on
        data_folder = "data/"
        data_fname = (
            data_folder
            + datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            + "_qos-"
            + str(userdata["qos"])
            + f"_netcond-{userdata['net_cond']}"
            + ".json"
        )
        if not os.path.isdir(data_folder):
            os.mkdir(data_folder)
        with open(data_fname, "w") as data_f:
            json.dump(data, data_f)

        # Stop disconnect thread, blocks until disconnect thread has been stopped
        if userdata["disconnect_thread"] is not None:
            print("Cancelling timer...")
            userdata["disconnect_event"].set()
            userdata["disconnect_thread"].join()

        # Process collected data
        if data:
            print("Calculating statistics...")
            total_diff = 0
            data_points: List[int] = []
            for pkt in data:
                if pkt["seq_num"] != -1:
                    total_diff += pkt["time_diff"]
                    data_points.append(pkt["time_diff"])

            std_deviation = statistics.stdev(data_points)
            max_point = max(data_points)
            min_point = min(data_points)
            median = statistics.median(data_points)

            with open(stats_fname, "a") as stats_f:
                stats_f.write("Subscriber\n")
                stats_f.write("----------\n")
                stats_f.write(f"Start time: {start_time}\n")
                stats_f.write(f"Network conditions: {userdata['net_cond']}\n")
                stats_f.write(f"QoS level: {userdata['qos']}\n")
                stats_f.write(f"Data file: {data_fname}\n")
                stats_f.write(f"Number of packets sent: {userdata['total_packets']}\n")
                stats_f.write(f"Number of packets received: {len(data_points)}\n")
                stats_f.write(f"Packet loss: {(N-len(data_points))/N*100}%\n")
                stats_f.write(f"---End-to-End Delay\n")
                stats_f.write(f"Min: {min_point}ms\n")
                stats_f.write(f"Mean: {total_diff/len(data)}ms\n")
                stats_f.write(f"Median: {median}ms\n")
                stats_f.write(f"Max: {max_point}ms\n")
                stats_f.write(f"Standard Deviation: {std_deviation}\n")
                stats_f.write("\n\n")

        print("Subscriber closed successfully")
