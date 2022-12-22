"""
Program for integrating audio/video into a meeting
Handles only up to one audio and one video stream in one meeting
"""
import signal
import threading
from types import FrameType
from typing import NoReturn
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.support.ui import Select
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions

import sys
import time
import subprocess
import os
import shlex
import argparse
import logging

CAMERA_NAME = "virtual_camera"
MIC_NAME = "virtual_mic"
RUNNING = True
driver = None
CAMERA_READY = False
ffplay_pid = 0
ffmpeg_pid = 0
MANUAL_MUTE = False


def exit_program() -> NoReturn:
    """
    Free all resources and exit the program

    Returns:
        NoReturn: Does not return, since the program exits
    """
    logging.info("Exiting cam_integration!")
    global RUNNING
    RUNNING = False
    if ffplay_pid:
        try:
            os.kill(ffplay_pid, signal.SIGKILL)
        except OSError:
            logging.warning("ffplay could not be killed, "
                            "maybe already killed")
    if ffmpeg_pid:
        try:
            os.kill(ffmpeg_pid, signal.SIGKILL)
        except OSError:
            logging.warning("ffmpeg could not be killed, "
                            "maybe already killed")
    driver.quit()
    sys.exit(0)


def signal_handler(sig: int, frame: FrameType) -> None:
    """
    Gets called when SIGINT is sent to process, calls exit_program()

    Args:
        sig (int): Signal given to signal_handler, can only be SIGINT
        frame (FrameType): Execution frame
    """
    logging.info("Received SIGINT")
    exit_program()


def create_loopback_device(device_number: int, camera_name: str) -> None:
    """
    Uses the v4l2loopback module to create a virtual camera device
    Removes previous v4l2loopback modules, as only one can be active at a time

    Args:
        device_number (int): video device numver of the virtual device
        camera_name (str): name of the virtual camera
    """
    subprocess.run("sudo modprobe -r v4l2loopback", shell=True)
    time.sleep(1)
    subprocess.run(f'sudo modprobe v4l2loopback video_nr={device_number}'
                   f' card_label="{camera_name}" exclusive_caps=1', shell=True)
    time.sleep(1)


def manage_ffmpeg(
        video_stream: str, audio_stream: str, device_number: int) -> None:
    """
    Starts the ffmpeg/ffplay proesses to retrieve the rtsp/audio stream
    Monitors the cpu usage of the ffmpeg/ffplay processes
    and restarts them if needed

    Args:
        video_stream (str): url of the video stream
        audio_stream (str): url of the audio stream
        device_number (int): video device number of the virtual device
    """
    global ffmpeg_pid
    global ffplay_pid

    if video_stream:
        ffmpeg_pid = get_video_stream(video_stream, device_number)
    if audio_stream:
        ffplay_pid = get_audio_stream(audio_stream)

    while RUNNING:
        if video_stream and not monitor_process(ffmpeg_pid, 1.0):
            logging.error("Restarting ffmpeg!")
            try:
                os.kill(ffmpeg_pid, signal.SIGKILL)
            except OSError:
                logging.warning("ffmpeg could not be killed, "
                                "maybe already killed")
            ffmpeg_pid = get_video_stream(video_stream, device_number)

        if audio_stream and not monitor_process(ffplay_pid, 1.0):
            logging.error("Restarting ffplay!")
            try:
                os.kill(ffplay_pid, signal.SIGKILL)
            except OSError:
                logging.warning("ffplay could not be killed, "
                                "maybe already killed")
            ffplay_pid = get_audio_stream(audio_stream)
        time.sleep(10)
    if ffmpeg_pid:
        try:
            os.kill(ffmpeg_pid, signal.SIGKILL)
        except OSError:
            logging.warning("ffmpeg could not be killed, "
                            "maybe already killed")
    if ffplay_pid:
        try:
            os.kill(ffplay_pid, signal.SIGKILL)
        except OSError:
            logging.warning("ffplay could not be killed, "
                            "maybe already killed")


def monitor_process(pid: int, threshold: float) -> bool:
    """
    Uses pidstat to monitor the current cpu usage of the process
    If it is under the threshold, False is returned

    Args:
        pid (int): pid of the process to be monitored
        threshold (float): threshold for cpu usage

    Returns:
        bool: True, if the process cpu usage is above the threshold,
              False otherwise
    """
    logging.debug(f"Monitoring {pid}!")

    if not pid:
        # invalid pid
        logging.info("Pid is invalid!")
        return False

    monitoring_command = f"pidstat -p {pid} 3 1 | tail -1 | awk '{{print $8}}'"
    monitoring_result = subprocess.run(monitoring_command, shell=True,
                                       capture_output=True, text=True)

    try:
        cpu_usage = float(monitoring_result.stdout.replace(",", "."))
    except ValueError:
        return False
    logging.debug(cpu_usage)

    if cpu_usage < threshold:
        logging.error(f"There is a problem with process {pid}!")
        return False

    return True


def get_video_stream(stream_url: str, device_number: int) -> int:
    """
    Uses ffmpeg to retrieve the rtsp stream and
    play it into the virtual camera device

    Args:
        stream_url (str): url of the video stream
        device_number (int): video device number for the virtual camera

    Returns:
        int: pid of the created ffmpeg process
    """
    command = f"ffmpeg -loglevel error -rtsp_transport tcp -i {stream_url} -f"\
              f" v4l2 -vcodec rawvideo -pix_fmt yuv420p"\
              f" /dev/video{device_number}"
    if RUNNING:
        ffmpeg_proc = subprocess.Popen(shlex.split(command),
                                       stdin=subprocess.PIPE, shell=False)
    else:
        return 0

    result = None
    while RUNNING:
        result = subprocess.run("v4l2-ctl --device=10 --all | "
                                "grep 'Size Image' head -1 |awk '{print $4}'",
                                capture_output=True, shell=True)
        logging.debug(f"Current size image: {result.stdout}")
        if result.stdout != b"0\n":
            break
        time.sleep(1)

    logging.debug(f"Result of v4l2-ctl command: {result.stdout}")

    global CAMERA_READY
    CAMERA_READY = True

    logging.info(f"ffmpeg PID: {ffmpeg_proc.pid}")

    return ffmpeg_proc.pid


def get_audio_stream(stream_url: str) -> int:
    """
    Uses ffplay to play the sound of the rtsp stream
    This sound should be picked up by the virtual mic

    Args:
        stream_url (str): url of the audio stream

    Returns:
        int: pid of the created ffplay process
    """
    command = f"ffplay -loglevel error -rtsp_transport"\
              f" tcp -nodisp {stream_url}"
    if RUNNING:
        ffplay_proc = subprocess.Popen(shlex.split(command), shell=False)
    else:
        return 0
    logging.info(f"ffplay PID: {ffplay_proc.pid}")

    return ffplay_proc.pid


def create_virtual_mic(microphone_name: str) -> None:
    """
    Create virtual microphone for audio playback

    Args:
        microphone_name (str): name the virtual microphone should get
    """
    subprocess.run("pactl unload-module module-remap-source", shell=True)
    subprocess.run("pactl unload-module module-null-sink", shell=True)

    null_sink_cmd = "pactl load-module module-null-sink sink_name=virtmic"\
                    " sink_properties=device.description="\
                    "Virtual_Microphone_Sink"
    module_null_sink_output = subprocess.run(null_sink_cmd, shell=True,
                                             capture_output=True, text=True)

    remap_source_cmd = f"pactl load-module module-remap-source master=virtmic"\
                       f".monitor source_name=virtmic source_properties="\
                       f"device.description={microphone_name}"
    module_remap_source_output = subprocess.run(remap_source_cmd, shell=True,
                                                capture_output=True, text=True)

    logging.info(f"module-null-sink: {module_null_sink_output.stdout}")
    logging.info(f"module-remap-source: {module_remap_source_output.stdout}")


def wait_for(element: tuple, timeout: int = 10) -> None:
    """
    Wait until given element is present and clickable

    Args:
        element (tuple): element to be waited for
        timeout (int, optional): maximum time to wait. Defaults to 10.
    """
    WebDriverWait(driver, timeout).until(
            expected_conditions.presence_of_element_located(element))
    WebDriverWait(driver, timeout).until(expected_conditions
                                         .element_to_be_clickable(element))


def click_button_xpath(button_xpath: str) -> None:
    """
    Clicks button given by the xpath of the button.

    Args:
        button_xpath (str): xpath of the button to be clicked
    """
    try:
        # wait for button to be clickable and the cllick it
        wait_for((By.XPATH, button_xpath))
        element = driver.find_element(by=By.XPATH, value=button_xpath)
        driver.execute_script("arguments[0].click();", element)
    except NoSuchElementException:
        logging.critical(f"Button with XPath: {button_xpath} "
                         "not found! Aborting.")
        driver.quit()
        exit(-1)


def fill_input_xpath(input_xpath: str, input: str) -> None:
    """
    Fills the input field given by its xpath with input.

    Args:
        input_xpath (str): xpath of the input field
        input (str): text to be input into the field
    """
    try:
        # wait for input field to be available and the fill it with the input
        wait_for((By.XPATH, input_xpath))
        driver.find_element(by=By.XPATH, value=input_xpath).send_keys(input)
    except NoSuchElementException:
        logging.critical(f"Input with XPath: {input_xpath} not found! "
                         "Aborting.")
        driver.quit()
        exit(-1)


def select_option_by_value(select_xpath: str, option_value: str) -> None:
    """
    Selects option given by option_text in dropdown menu given by its xpath

    Args:
        select_xpath (str): xpath of the dropdown menu
        option_value (str): value of the option to be selected
    """
    wait_for((By.XPATH, select_xpath))
    select = Select(driver.find_element(by=By.XPATH, value=select_xpath))
    select.select_by_value(option_value)


def select_option(select_xpath: str, option_text: str) -> None:
    """
    Selects option given by option_text in dropdown menu given by its xpath

    Args:
        select_xpath (str): xpath of the dropdown menu
        option_text (str): text of the option to be selected
    """
    wait_for((By.XPATH, select_xpath))
    select = Select(driver.find_element(by=By.XPATH, value=select_xpath))
    select.select_by_visible_text(option_text)


def select_last_option(select_xpath: str) -> None:
    """
    Selects last option in dropdown menu given by its xpath

    Args:
        select_xpath (str): xpath of the dropdown menu
    """
    wait_for((By.XPATH, select_xpath))
    select = Select(driver.find_element(by=By.XPATH, value=select_xpath))
    num_options = len(select.options)
    select.select_by_index(num_options - 1)


def check_microphone_muted() -> bool:
    """
    Check if microphone is muted by inspecting the mute/unmute button

    Returns:
        bool: True, if microphone is muted, False otherwise
    """
    try:
        umute_button_xpath = '//*[@aria-label="Unmute"]'
        driver.find_element(by=By.XPATH, value=umute_button_xpath)
        # Unmute button exists, therefore currrently muted
        return True
    except NoSuchElementException:
        # Unmute button does not exist, therefore not muted
        return False


def mute_microphone() -> None:
    """
    Mute microphone if not currently muted
    """

    if not check_microphone_muted():
        mute_button_xpath = '//*[@aria-label="Mute"]'
        click_button_xpath(mute_button_xpath)


def unmute_microphone() -> None:
    """
    Unmute microphone if currently muted
    """
    if check_microphone_muted():
        unmute_button_xpath = '//*[@aria-label="Unmute"]'
        click_button_xpath(unmute_button_xpath)


def toggle_microphone() -> None:
    """
    Mute microphone if unmuted, unmute microphone if muted
    """
    if check_microphone_muted():
        unmute_microphone()
    else:
        mute_microphone()


def get_moderator_chat_partner() -> WebElement:
    """
    Get first moderator chat partner from list of chat partners

    Returns:
        WebElement: Element that opens private chat with moderator
    """
    # list chat participants
    userlist_xpath = '//*[@data-test="userListContent"]'
    userlist = driver.find_element(by=By.XPATH, value=userlist_xpath)

    chatlist_xpath = './/*[@role="tabpanel"]//*[@data-test="moderatorAvatar"]'

    if chat_partners := userlist.find_elements(
        by=By.XPATH, value=chatlist_xpath
    ):
        return chat_partners[0]

    return None


def get_last_chat_message() -> str:
    """
    Get last chat message of current private chat

    Returns:
        str: Text in last message
    """
    messages_xpath = './/*[@data-test="chatUserMessageText"]'
    messages = driver.find_elements(by=By.XPATH, value=messages_xpath)

    # at least one message, as there would not be a chat otherwise
    return messages[-1].text


def close_chat() -> None:
    """
    Close current private chat
    """
    close_chat_xpath = '//*[@data-test="closePrivateChat"]'
    click_button_xpath(close_chat_xpath)
    time.sleep(0.5)


def check_chats() -> None:
    """
    Check, whether there is a new message from a moderator and execute the
    command in the message
    """
    chat_partner = get_moderator_chat_partner()

    if not chat_partner:
        return

    # open chat
    chat_partner.click()
    time.sleep(1)

    message = get_last_chat_message()
    execute_command(message)
    close_chat()


def send_chat_message(message: str) -> None:
    """
    Send the given message to the current chat partner

    Args:
        message (str): Message to be sent
    """
    text_input_xpath = '//*[@id="message-input"]'
    fill_input_xpath(text_input_xpath, message)
    send_button_xpath = '//*[@aria-label="Send message"]'
    click_button_xpath(send_button_xpath)


def send_chat_help() -> None:
    """
    Send a help message to current chat partner
    """
    send_chat_message("Supported commands:")
    send_chat_message("/mute: Mute the audio.")
    send_chat_message("/unmute: Unmute the audio.")
    send_chat_message("/togglemic: Toggle audio mute. Mute if not currently "
                      "muted, unmute if currently muted.")
    send_chat_message("/unshare_cam: Disable video stream.")
    send_chat_message("/share_cam: Enable video stream (If video stream "
                      "is provided).")
    send_chat_message("/extend_meeting <minutes>: "
                      "Extend meeting by <minutes> minutes.")
    time.sleep(1)


def extend_meeting(minutes: int) -> None:
    """
    Extend the current meeting

    Args:
        minutes (int): Minutes to extend the meeting
    """
    global STOP_TIME
    max_minutes = int((MAX_STOP_TIME - STOP_TIME) // 60)

    if minutes <= max_minutes:
        STOP_TIME += minutes * 60
        send_chat_message(f"Meeting will be extended by {minutes} minutes!")
    else:

        send_chat_message("Meeting cannot be extended because of overlaps in "
                          "the schedule! Maximum extension is "
                          f"{max_minutes} minutes!")
        time.sleep(0.5)


def execute_command(command: str) -> None:
    """
    Execute the given command

    Args:
        command (str): Command to execute
    """
    global MANUAL_MUTE
    if command == "/mute":
        MANUAL_MUTE = True
        mute_microphone()
    elif command == "/unmute":
        MANUAL_MUTE = True
        unmute_microphone()
    elif command == "/togglemic":
        MANUAL_MUTE = True
        toggle_microphone()
    elif command == "/unshare_cam":
        unshare_camera()
    elif command == "/share_cam":
        share_camera()
    elif command == "/help":
        send_chat_help()
    elif "/extend_meeting" in command:
        try:
            minutes = int(command[16:])
            extend_meeting(minutes)
        except ValueError:
            send_chat_message("Please provide a valid number of minutes after "
                              "the command.")
            time.sleep(0.5)
    else:
        send_chat_help()


def check_camera_shared() -> bool:
    """
    Check if camera is currently shared by inspecting the share camera button

    Returns:
        bool: True, if currently sharing camera
    """
    try:
        unshare_button_xpath = '//*[@aria-label="Stop sharing webcam"]'
        driver.find_element(by=By.XPATH, value=unshare_button_xpath)
        # Stop sharing button exists, therefore currrently sharing camera
        return True
    except NoSuchElementException:
        # Stop sharing button does not exist, therefore not sharing camera
        return False


def unshare_camera() -> None:
    """
    Disable camera sharing
    """
    if not check_camera_shared():
        # not currently sharing camera
        return

    unshare_camera_xpath = '//*[@aria-label="Stop sharing webcam"]'
    click_button_xpath(unshare_camera_xpath)


def share_camera() -> None:
    """
    Share camera
    """
    if check_camera_shared():
        # already sharing camera
        return

    share_camera_xpath = '//*[@aria-label="Share webcam"]'
    click_button_xpath(share_camera_xpath)
    time.sleep(5)

    # select the virtual camera for sharing
    select_camera_xpath = '//*[@id="setCam"]'
    select_option(select_camera_xpath, CAMERA_NAME)
    time.sleep(2)

    # select video quality for sharing the camera
    if VIDEO_QUALITY:
        select_quality_xpath = '//*[@id="setQuality"]'
        select_option_by_value(select_quality_xpath, VIDEO_QUALITY)
        time.sleep(3)

    # start sharing the camera
    start_sharing_xpath = '//*[@aria-label="Start sharing"]'
    click_button_xpath(start_sharing_xpath)
    time.sleep(1)


def check_stop_time() -> None:
    if time.time() > STOP_TIME:
        exit_program()


def integrate_camera(
        room_url: str, name: str, infrastructure: str,
        video_stream: str, audio_stream: str, access_code: str) -> NoReturn:
    """
    Integrate video and/or audio into a meeting

    Args:
        room_url (str): url of the meeting
        name (str): name to be displayed as participant
        infrastructure (str): type of infrastructure used for the meeting room
        video_stream (str): url of the video stream (can be None)
        audio_stream (str): url of the audio stream (can be None)
        access_code (str): access code for access as moderator (can be None)

    Returns:
        NoReturn: Does not return, but stays in the function
    """

    logging.debug(f"video stream: {video_stream}")
    logging.debug(f"audio stream: {audio_stream}")
    # create and initialize audio resources
    if audio_stream:
        create_virtual_mic(MIC_NAME)

    # initialize video resources, i.e., the virtual device and ffmpeg process
    if video_stream:
        create_loopback_device(10, CAMERA_NAME)
    ffmpeg_thread = threading.Thread(target=manage_ffmpeg,
                                     args=(video_stream, audio_stream, 10))
    ffmpeg_thread.start()

    time.sleep(5)

    # get chrome options and add argument for granting camera permission
    # and window maximization
    options = webdriver.ChromeOptions()
    options.add_argument("--use-fake-ui-for-media-stream")
    options.add_argument("--start-maximized")
    options.add_argument("--headless")

    global driver
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()),
                              options=options)

    # go to initial website
    driver.get(room_url)

    time.sleep(1)

    if infrastructure == "greenlight":

        # get field for entering the name of the user
        enter_name_xpath = '//*[@placeholder="Enter your name!"]'
        fill_input_xpath(enter_name_xpath, name)

        time.sleep(1)

        # click the join button to join the meeting
        join_room_xpath = '//*[@id="room-join"]'
        click_button_xpath(join_room_xpath)

        time.sleep(3)

    elif infrastructure == "studip":
        enter_name_xpath = '//*[@name="name"]'
        fill_input_xpath(enter_name_xpath, name)

        time.sleep(1)

        if access_code:
            # get field for entering access code
            access_code_xpath = '//*[@name="password"]'
            logging.info(f"Access code: {access_code}")
            fill_input_xpath(access_code_xpath, access_code)
            time.sleep(1)

        # click the join button to join the meeting
        join_room_xpath = '//*[@name="accept"]'
        click_button_xpath(join_room_xpath)

        time.sleep(3)

    else:
        logging.critical("Wrong infrastructure parameter set!")
        exit_program()

    # if the meeting is not started yet, the url does not change
    # therefore wait until url changes
    while driver.current_url == room_url:
        logging.warning("Waiting for meeting to start!")
        time.sleep(5)

    time.sleep(1)

    # go into listen only mode
    # listenOnly_xpath ='//*[@class="icon--2q1XXw icon-bbb-listen"]'
    # click_button_xpath(listenOnly_xpath)

    if audio_stream:
        # activate microphone
        microphone_xpath = '//*[@aria-label="Microphone"]'
        click_button_xpath(microphone_xpath)
    else:
        # go into listen only mode
        listen_only_xpath = '//*[@aria-label="Listen only"]'
        click_button_xpath(listen_only_xpath)

    time.sleep(10)

    if video_stream:
        while not CAMERA_READY:
            time.sleep(1)

    # click the share camera button to open the sharing dialogue
    if video_stream:
        share_camera()

    if audio_stream:
        # expand list for changing audio devices
        change_audio_device_xpath = '//*[@aria-label="Change audio device"]'
        click_button_xpath(change_audio_device_xpath)
        time.sleep(1)

        # choose virtual microphone by its given name
        micname_xpath = f"//*[contains(text(),'{MIC_NAME}')]"
        click_button_xpath(micname_xpath)

    while True:
        check_stop_time()
        check_chats()
        if audio_stream and not MANUAL_MUTE:
            unmute_microphone()
        time.sleep(0.1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, filename="cam_integration.log",
                        filemode="a",
                        format="%(asctime)s - %(levelname)s - %(message)s")

    signal.signal(signal.SIGINT, signal_handler)

    parser = argparse.ArgumentParser()

    parser.add_argument("room_url", help="URL of the meeting room")
    parser.add_argument("id", help="Name to be displayed in the meeting")
    parser.add_argument("infrastructure",
                        help="Infrastructure used for the meeting room")
    parser.add_argument("regular_stop_time", type=float,
                        help=" Regular time for process to end")
    parser.add_argument("max_stop_time", type=float,
                        help="Latest allowed time for process to end")
    parser.add_argument("--audio", help="URL of the audio stream")
    parser.add_argument("--video", help="URL of the video stream")
    parser.add_argument("--code", help="Access code for joining as moderator")
    parser.add_argument("--video_quality",
                        help="Video quality to select for the stream")

    args = parser.parse_args()

    room_url = args.room_url
    name = args.id
    infrastructure = args.infrastructure
    audio_stream = args.audio
    video_stream = args.video
    access_code = args.code
    global VIDEO_QUALITY
    VIDEO_QUALITY = args.video_quality

    regular_stop_time = args.regular_stop_time
    max_stop_time = args.max_stop_time

    global STOP_TIME
    STOP_TIME = regular_stop_time
    global MAX_STOP_TIME
    MAX_STOP_TIME = max_stop_time

    integrate_camera(room_url, name, infrastructure,
                     video_stream, audio_stream, access_code)
