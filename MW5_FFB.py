"""A script to take telemetry from Mechwarrior 5: mercs and inject it as force feedback effect into a Joystick.

Current only supports the Microsoft Sidewinder Force Feedback 2, as that is
the stick I have :-D. To read it and send force feedback effects, I wrote my
own python bindings in c/pybind11. https://github.com/HappyFox/SidewinderFFB2

To get the telemetry, it uses the SpaceMonkey program by PHARTGAMES. See
https://github.com/PHARTGAMES/SpaceMonkey

MW5 has issues reading the Sidewinder Force Feedback 2, so the script uses a
virtual joystick, vjoy : https://github.com/njz3/vJoy/ . To write to the
virtual joystick, I use the pyvjoy library. https://github.com/tidzo/pyvjoy

This also allowed me to change the pov hat into discreet buttons, as MW5 seems
to like that better.

Also the "6" button is now a "layer" button, giving access to another set when
held. Except "7" and "8", when "6" is held those increase and decrease the force
feedback gain.

This is verging on needing to be split up into seperate files/modules. But it's
basically complete.
"""

from __future__ import annotations

import asyncio
import dataclasses
import math
import signal
import struct
import sys
import time
from dataclasses import dataclass, field

import pyvjoy
import SidewinderFFB2
from rich import print
from rich.spinner import Spinner
from rich.text import Text
from SidewinderFFB2 import DI_FFNOMINALMAX as FF_MAX

ROLLING_AVERAGE_LEN = 5


def map_axis(val: int) -> int:
    return val * 0x8000 // 0xFFFF


pov_idx = [0, 4500, 9000, 13500, 18000, 22500, 27000, 31500]


@dataclass
class JoyStick:
    x: int = None
    y: int = None
    rudder: int = None
    throttle: int = None


@dataclass
class State:
    telm_times: list[int] = dataclasses.field(default_factory=list)
    long_g: float = None
    late_g: float = None
    joy: JoyStick = field(default_factory=JoyStick)


@dataclass
class Settings:
    gain: int
    gain_set: bool
    running: bool


THROTTLE_DEAD_START = int(0x4000 * 0.95)
THROTTLE_DEAD_STOP = int(0x4000 * 1.05)


async def joy_poller(settings: Settings, state: State) -> None:
    vjoy = pyvjoy.VJoyDevice(1)
    vjoy.reset()

    buzzer = SidewinderFFB2.BuzzForce()

    throttle_dead = False

    gain_up = False
    gain_down = False

    while settings.running:
        await asyncio.sleep(
            0.03
        )  # Yeild to let other tasks, but also to let the UI thread run on the game.
        joy_state = SidewinderFFB2.poll()

        layer = joy_state.buttons[5]
        buttons = list(joy_state.buttons)
        del buttons[5]

        start = 0
        if layer:
            start = 15
            if buttons[-1]:
                if not gain_up:
                    settings.gain += 500
                    settings.gain = min(settings.gain, SidewinderFFB2.DI_FFNOMINALMAX)
                    settings.gain_set = False
                    gain_up = True
            elif buttons[-2]:
                if not gain_down:
                    settings.gain -= 500
                    settings.gain = max(settings.gain, 0)
                    settings.gain_set = False
                    gain_down = True
            else:
                gain_down = False
                gain_up = False

        but_val = 0
        for idx, but_state in enumerate(buttons, start=start):
            if but_state:
                but_val += 1 << idx

        state.joy.x = joy_state.x
        state.joy.y = joy_state.y
        state.joy.rudder = joy_state.r_z
        state.joy.throttle = joy_state.throttle

        vjoy.data.wAxisX = map_axis(joy_state.x)
        vjoy.data.wAxisY = map_axis(joy_state.y)
        vjoy.data.wAxisZRot = map_axis(joy_state.r_z)

        throttle = map_axis(joy_state.throttle)

        if THROTTLE_DEAD_START < throttle < THROTTLE_DEAD_STOP:
            vjoy.data.wAxisZ = 0x4000
            if not throttle_dead:
                buzzer.start()
                throttle_dead = True
        else:
            vjoy.data.wAxisZ = map_axis(joy_state.throttle)
            throttle_dead = False

        if joy_state.pov is not None:
            but_val += 1 << (pov_idx.index(joy_state.pov) + (7 + start))

        vjoy.data.lButtons = but_val

        vjoy.update()


class TelemetryProtocol(asyncio.DatagramProtocol):
    def __init__(self, state: State):
        self.state = state

    def datagram_received(self, data, addr):
        self.state.telm_times.append(time.time_ns())
        self.state.telm_times = self.state.telm_times[-ROLLING_AVERAGE_LEN:]

        self.state.late_g, self.state.long_g, _ = struct.unpack("<fff", data[68:80])


async def force_feed_back(settings: Settings, state: State) -> None:
    SidewinderFFB2.acquire()
    x_y_force = SidewinderFFB2.ConstantForce()
    x_y_force.set_gain(7000)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, x_y_force.set_direction, 0, 0)

    while settings.running:
        if not settings.gain_set:
            await loop.run_in_executor(None, x_y_force.set_gain, settings.gain)
            settings.gain_set = True

        if state.long_g:
            lat_dir = max(-FF_MAX, min(FF_MAX, int(math.tanh(state.late_g) * FF_MAX)))
            long_dir = max(-FF_MAX, min(FF_MAX, -int(math.tanh(state.long_g) * FF_MAX)))

            await loop.run_in_executor(None, x_y_force.set_direction, lat_dir, long_dir)
        else:
            await asyncio.sleep(0)


async def display(settings: Settings, state: State) -> None:
    spin = Spinner("material", text="test", style="green")

    while settings.running:
        telemetry_lat = Text("N/A", "bold red")

        if state.telm_times:
            lowest = state.telm_times[0]
            avg = sum([x - lowest for x in state.telm_times]) / len(state.telm_times)

            time_since_telm = (
                max(time.time_ns() - lowest, avg) / 1000000000
            )  # ns to seconds

            color = "green"
            if time_since_telm > 1:
                color = "orange1"
            if time_since_telm < 2:
                telemetry_lat = Text(f"{time_since_telm:2.4f}", color)

        text = Text.assemble(
            "telm lat : ",
            telemetry_lat,
            f", X: {state.joy.x}. Y: {state.joy.y}, Rudder: {state.joy.rudder}, Throttle: {state.joy.throttle}, FF Gain: {settings.gain//100}%",
        )

        spin.update(text=text)

        print(spin.render(time.time()), end="\r")
        await asyncio.sleep(0.1)


async def main():
    # Set gain to 70%
    settings = Settings(gain=FF_MAX // 100 * 70, gain_set=False, running=True)
    state = State()

    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: TelemetryProtocol(state), local_addr=("127.0.0.1", 10001)
    )

    def signal_handler(sig, frame):
        settings.running = False

    signal.signal(signal.SIGINT, signal_handler)

    async with asyncio.TaskGroup() as tg:
        _ = tg.create_task(joy_poller(settings, state))
        _ = tg.create_task(force_feed_back(settings, state))
        _ = tg.create_task(display(settings, state))

    transport.close()


if __name__ == "__main__":
    try:
        try:
            SidewinderFFB2.init()
            SidewinderFFB2.acquire()
        except RuntimeError:
            print(
                "Unable to acquire the joystick, is it plugged in and the default joystick?",
            )
            sys.exit(1)
        print("All Systems Nominal.", end="\r")
        time.sleep(0.5)
        asyncio.run(main())
        print("\nShutdown")
    finally:
        SidewinderFFB2.release()
