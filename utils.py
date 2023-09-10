import hashlib
import json
import os
import random
import time
import asyncio
from datetime import datetime

import pandas as pd
import readtime
import requests
import streamlit as st
from annotated_text import annotated_text, annotation
from google.cloud.firestore import ArrayUnion
from llama_index import Document, ServiceContext, get_response_synthesizer
from llama_index.indices.document_summary import (
    DocumentSummaryIndex,
    DocumentSummaryIndexRetriever,
)
from llama_index.llms import OpenAI
from llama_index.query_engine import RetrieverQueryEngine
from streamlit_extras.customize_running import center_running
from streamlit_extras.row import row
from streamlit_extras.switch_page_button import switch_page

from config import NEWS_CATEGORIES


def hash_text(text: str):
    hash_object = hashlib.sha256(text.encode())
    return hash_object.hexdigest()


def redirect_button(url: str, text: str = None, color="#FD504D"):
    st.markdown(
        f"""
    <a href="{url}" target="_self">
        <div style="
            display: inline-block;
            padding: 0.5em 1em;
            color: #FFFFFF;
            background-color: {color};
            border-radius: 3px;
            text-decoration: none;">
            {text}
        </div>
    </a>
    """,
        unsafe_allow_html=True,
    )


def check_is_sign_up(username=""):
    if not username:
        return False

    user_ref = (
        st.session_state["firestore_db"].collection("authentication").document(username)
    )
    user_meta = user_ref.get()
    if user_meta.exists:
        return True
    else:
        return False


def sign_up(username, password, lastname, firstname, favorite):
    signup_info = ""
    is_signup = False
    try:
        doc_ref = (
            st.session_state["firestore_db"]
            .collection("authentication")
            .document(username)
        )
        id = hash_text(username)
        signup_info = {
            "username": username,
            "password": password,
            "last name": lastname,
            "first name": firstname,
            "id": id,
            "favorite": favorite,
        }
        doc_ref.set(signup_info)
        is_signup = True
        signup_info = "Successfully Sign Up! Welcome {} {} to NewsGPT".format(
            firstname.capitalize(), lastname.capitalize()
        )
    except Exception as e:
        signup_info = f"Fail to sign up: {e}"

    return is_signup, signup_info


def password_entered(username="", password=""):
    """Checks whether a password entered by the user is correct."""

    if not username or not password:
        st.session_state["password_correct"] = False

    user_ref = (
        st.session_state["firestore_db"].collection("authentication").document(username)
    )
    user_meta = user_ref.get()
    if user_meta.exists:
        user_meta = user_meta.to_dict()
        if user_meta["password"] == password:
            st.session_state["password_correct"] = True
            st.session_state["username"] = username
            st.session_state["realname"] = (
                user_meta["first name"].capitalize()
                + user_meta["last name"].capitalize()
            )
            st.session_state["is_auth_user"] = True
            st.session_state["user_ref"] = user_ref
        else:
            st.session_state["password_correct"] = False
            st.session_state["is_auth_user"] = False
    else:
        st.session_state["password_correct"] = False
        st.session_state["is_auth_user"] = False


def signout():
    st.session_state["is_auth_user"] = False
    st.session_state["password_correct"] = False
    st.session_state["username"] = ""
    st.session_state["realname"] = ""
    st.session_state["active_summary_result"] = {}
    st.session_state["page_name"] = "login"


@st.cache_data
def load_activities(activities):
    df = pd.DataFrame(activities)
    df["date"] = pd.to_datetime(df["date"])
    df["y"] = df["date"].dt.year
    df["m"] = df["date"].dt.month
    df["d"] = df["date"].dt.day
    df["read cnt"] = 1
    group_by_time = df[["y", "m", "d", "read cnt"]].groupby(["y", "m", "d"]).sum()
    group_by_time = group_by_time.reset_index()
    group_by_time["yyyy-mm-dd"] = (
        group_by_time["y"].astype(str)
        + "-"
        + group_by_time["m"].astype(str)
        + "-"
        + group_by_time["d"].astype(str)
    )
    group_by_cat = df[["category", "read cnt"]].groupby(["category"]).sum()
    group_by_cat = group_by_cat.reset_index()
    return group_by_time, group_by_cat


async def recommendation(key, positive, daterange, limit, thresh, negative=[]):
    if key in ["activities", "positive"]:
        act_positive = [activity["id"] for activity in positive[-30:]]
        act_negative = [activity["id"] for activity in negative[-30:]]
        data = {
            "p": act_positive,
            "n": act_negative,
        }
        params = {"dr": daterange, "l": int(limit * 1.5), "t": thresh}
        data = json.dumps(data)
        response = requests.post(
                        f"{os.environ['QDRANT_LAMBDA_ENTRYPOINT']}/api/v1/recommend",
                        params=params,
                        data=data
                    )
    else:
        params = {
                    "c": key.lower(),
                    "dr": int(daterange),
                    "l": limit,
                }
        response = requests.get(
                        f"{os.environ['QDRANT_LAMBDA_ENTRYPOINT']}/api/v1/scroll",
                        params=params,
                    )
    return response

# TODO: better recommendation system (mixing the activities, positive and negative)
# TODO: Wrap the logics to the API
async def load_feeds_merge(total_articles=12, data_range=14, num_query_per_cat=10, thresh=0.1):
    st.session_state["recommend"] = []
    _, col2, _ = st.columns([0.1, 0.8, 0.1])
    with col2.status("Start Recommendation ...", expanded=True) as status:
        if not st.session_state["password_correct"]:
            st.session_state.page_name = "login"

        # Get User Metadata
        user_meta = st.session_state["user_ref"].get()
        user_meta = user_meta.to_dict()
        st.session_state["user_favorite"] = user_meta["favorite"]    
        
        positive, negative, activities = user_meta.get("positive", []), user_meta.get("negative", []), user_meta.get("activities", [])
        numAct, numPos, numNeg = len(activities), len(positive), len(negative)

        states = []
        if numAct < 10:
            states = st.session_state["user_favorite"]
        else:
            if numAct:
                states += ["activities"]
            # if numPos:
            #     states += ["positive"]
        tasks = []
        start = time.time()
        print(states)
        st.write("Searching for suitable news ...")
        for state in states:
            if state == "activities":
                pos = activities
            elif state == "positive":
                pos = positive
            else:
                pos = []
            tasks.append(recommendation(key=state, 
                                        positive=pos, 
                                        daterange=data_range, 
                                        limit=num_query_per_cat, 
                                        thresh=thresh, 
                                        negative=negative))
        print("Run Recommendation: {}".format(time.time()-start))
        start = time.time()
        responses = await asyncio.gather(*tasks)
        tmp_articles = []
        for response in responses:
            if response.status_code == 200:
                response = response.json()
                articles = response["result"]["articles"]
                tmp_articles.extend(articles)
            else:
                print(response.text)
        print("Unpack: {}".format(time.time()-start))
        
        final_articles, unique_id = list(), set()
        start = time.time()
        st.write("Unpacking results ...")
        for art in tmp_articles:
            if isinstance(art, str):
                try:
                    art = json.loads(art)
                except Exception as e:
                    st.toast(e)
                    return
            if art["payload"]["body"] == "" and art["payload"]["summary"] == "":
                continue
            cur_id = art["payload"]["id"]
            if cur_id not in unique_id:
                unique_id.add(cur_id)
                final_articles.append(art)
        st.session_state["recommend"].extend(
            random.sample(final_articles, min(len(final_articles), total_articles))
        )
        print("Reorganize: {}".format(time.time() - start))
        status.update(label="Recommendation Complete!", state="complete", expanded=False)

def load_feeds(total_articles=12, data_range=14, num_query_per_cat=10, thresh=0.1):
    # Login user, not guest
    st.session_state["recommend"] = []
    if st.session_state["password_correct"]:
        start = time.time()
        user_meta = st.session_state["user_ref"].get()
        user_meta = user_meta.to_dict()
        print("retrieve data from firestore: {}".format(time.time()-start))
        st.session_state["user_favorite"] = user_meta["favorite"]
        if not st.session_state["user_favorite"]:
            st.session_state["user_favorite"] = NEWS_CATEGORIES
        positive = user_meta.get("positive", [])
        negative = user_meta.get("negative", [])
        activities = user_meta.get("activities", [])
        numAct = len(activities)
        numPos = len(positive)
        tmp_articles = []
        # per_article_count = total_articles // len(st.session_state["user_favorite"])
        if (not positive and not activities) or (numPos < 30 and numAct < 30):
            # TODO: Parse all the favorite category all at once
            start = time.time()
            for fav in st.session_state["user_favorite"]:
                params = {
                    "c": fav.lower(),
                    "dr": int(data_range),
                    "l": int(num_query_per_cat),
                }

                response = requests.get(
                    f"{os.environ['QDRANT_LAMBDA_ENTRYPOINT']}/api/v1/scroll",
                    params=params,
                )

                # # Make the GET request
                # response = requests.get(url, params=params)
                if response.status_code != 200:
                    st.session_state["page_name"] = "feed"
                    st.session_state[
                        "error"
                    ] = f"load_feeds, qdrant scroll error: {response.text}"
                    print(response)
                    print(response.text)
                    print(response.json)
                    # print(merged_headers)
                    return
                response = response.json()
                articles = response["result"]["articles"]
                tmp_articles.extend(articles)
            print("retrieve all fav data from qdrant: {}".format(time.time()-start))

        elif numPos >= 30:
            print("recommend through positive")
            act_positive = [activity["id"] for activity in positive[-30:]]
            act_negative = [activity["id"] for activity in negative[-30:]]
            data = {
                "p": act_positive,
                "n": act_negative,
            }
            params = {"dr": data_range, "l": int(total_articles * 1.5), "t": thresh}
            data = json.dumps(data)
            response = requests.post(
                f"{os.environ['QDRANT_LAMBDA_ENTRYPOINT']}/api/v1/recommend",
                params=params,
                data=data,
            )  # , headers=headers)
            if response.status_code != 200:
                st.session_state["page_name"] = "feed"
                st.session_state[
                    "error"
                ] = f"load_feeds, qdrant recommend error: {response.text}"
                return
            response = response.json()
            articles = response["result"]["articles"]
            tmp_articles.extend(articles)

        elif numAct >= 30:
            print("recommend through activities")
            act_positive = [activity["id"] for activity in activities[-30:]]
            act_negative = [activity["id"] for activity in negative[-30:]]
            data = {
                "p": act_positive,
                "n": act_negative,
            }
            params = {"dr": data_range, "l": int(total_articles * 1.5), "t": thresh}

            data = json.dumps(data)
            response = requests.post(
                f"{os.environ['QDRANT_LAMBDA_ENTRYPOINT']}/api/v1/recommend",
                params=params,
                data=data,
            )  # , headers=headers)
            if response.status_code != 200:
                st.session_state["page_name"] = "feed"
                st.session_state[
                    "error"
                ] = f"load_feeds, qdrant recommend error: {response.text}"
                return
            response = response.json()
            articles = response["result"]["articles"]
            tmp_articles.extend(articles)

        final_articles, unique_id = list(), set()
        start = time.time()
        for art in tmp_articles:
            if isinstance(art, str):
                try:
                    art = json.loads(art)
                except Exception as e:
                    st.toast(e)
                    return
            if art["payload"]["body"] == "" and art["payload"]["summary"] == "":
                continue
            cur_id = art["payload"]["id"]
            if cur_id not in unique_id:
                unique_id.add(cur_id)
                final_articles.append(art)
        st.session_state["recommend"].extend(
            random.sample(final_articles, min(len(final_articles), total_articles))
        )
        print("Reorganize: {}".format(time.time()-start))


def load_search_feed(search_msg, total_articles=20, data_range=14):
    st.session_state["recommend"] = []
    q_article_count = int(total_articles * 1.5)
    if st.session_state["password_correct"]:
        params = {"q": search_msg, "l": q_article_count, "t": 0.3, "dr": data_range}

        data = {
            "e": [],
        }
        # Convert to JSON
        data = json.dumps(data)
        response = requests.post(
            f"{os.environ['QDRANT_LAMBDA_ENTRYPOINT']}/api/v1/search",
            params=params,
            data=data,
        )
        response = response.json()
        articles = response["result"]["articles"]
        final_articles, unique_id = list(), set()
        for art in articles:
            if art["payload"]["body"] == "" and art["payload"]["summary"] == "":
                continue
            cur_id = art["payload"]["id"]
            if cur_id not in unique_id:
                unique_id.add(cur_id)
                final_articles.append(art)
        st.session_state["recommend"].extend(
            random.sample(final_articles, min(len(final_articles), total_articles))
        )


def load_cat_feed(category="World", total_articles=12, data_range=14):
    st.session_state["recommend"] = []
    q_article_count = int(total_articles * 1.5)
    if st.session_state["password_correct"]:
        params = {"c": category.lower(), "dr": data_range, "l": q_article_count}

        response = requests.get(
            f"{os.environ['QDRANT_LAMBDA_ENTRYPOINT']}/api/v1/scroll", params=params
        )  # , headers=headers)
        if response.status_code != 200:
            st.session_state["page_name"] = "feed"
            st.session_state[
                "error"
            ] = f"load_cat_feed, qdrant scroll error: {response.text}"
            return
        response = response.json()
        articles = response["result"]["articles"]
        final_articles, unique_id = list(), set()
        for art in articles:
            if art["payload"]["body"] == "" and art["payload"]["summary"] == "":
                continue
            cur_id = art["payload"]["id"]
            if cur_id not in unique_id:
                unique_id.add(cur_id)
                final_articles.append(art)
        st.session_state["recommend"].extend(
            random.sample(final_articles, min(len(final_articles), total_articles))
        )


def generate_feed_layout():
    # Add CSS for rounded corners
    st.markdown(
        """
    <style>
        .rounded-image img {
            border-radius: 10px; /* Adjust the corner roundness */
            width: 400px; /* Fixed width */
            height: 300px; /* Fixed height */
        }
    </style>
    """,
        unsafe_allow_html=True,
    )
    narticles = len(st.session_state["recommend"])
    col1, col2, col3 = st.columns([0.1, 0.8, 0.1])
    grids = []
    for _ in range(narticles):
        with col2:
            grids.append(row(2, vertical_align="top", gap="small"))
            st.divider()
            st.markdown("<br>", unsafe_allow_html=True)
    # grids = [col2.row(2, vertical_align="top", gap="medium") for _ in range(narticles)]
    start = time.time()
    for i in range(narticles):
        current_payload = st.session_state["recommend"][i]["payload"]
        if current_payload["summary"] == "" and current_payload["body"] == "":
            continue
        current_embedding = st.session_state["recommend"][i]["vector"]
        img_url = current_payload["top_image"]
        article_url = current_payload["url"]
        title = f"#### [{current_payload['title']}]({article_url})"
        # grids[i].image(img_url)
        grids[i].markdown(
            f"<div class='rounded-image'><img src='{img_url}' alt='Image'></div>",
            unsafe_allow_html=True,
        )
        with grids[i].container():
            if current_payload["summary"]:
                summary = current_payload["summary"][:300] + " ... "
            else:
                summary = current_payload["body"][:300] + " ... "
            st.markdown(title, unsafe_allow_html=True)
            st.caption("publish date: " + current_payload["date"])
            st.write(summary)

            # st.button(label="Summarize",
            #         key=current_payload['id']+str(random.randint(1000, 9999)),
            #         on_click=run_summary,
            #         kwargs={"payload": current_payload,
            #                 "query_embed": current_embedding,
            #                 "ori_article_id": st.session_state['recommend'][i]['id'],
            #                 "compare_num": 3}
            #                 )
            st.button(
                label="Chat with Articles",
                key=current_payload["id"] + str(random.randint(1000, 9999)),
                on_click=run_chat,
                kwargs={
                    "payload": current_payload,
                    "query_embed": current_embedding,
                    "ori_article_id": st.session_state["recommend"][i]["id"],
                    "compare_num": 3,
                },
            )

    print("generate_feed_layout: {}".format(time.time() - start))


def update_activities(
    title, id, category, summary_rt, ori_rt, ner_loc, ner_org, ner_per, chat_mode=False
):
    new = {
        "title": hash_text(title),
        "id": id,
        "category": category,
        "ner_loc": ner_loc,
        "ner_org": ner_org,
        "ner_per": ner_per,
        "date": datetime.now(),
        "readtime": round(
            (datetime.now() - st.session_state["read_start"]).total_seconds()
        )
        if not chat_mode
        else summary_rt,
        "pred_readtime": summary_rt,
    }
    user_meta = st.session_state["user_ref"].get()
    user_meta = user_meta.to_dict()
    if "activities" not in user_meta:
        cur_data = {"activities": [new]}
        st.session_state["user_ref"].set(cur_data, merge=True)
    else:
        st.session_state["user_ref"].update({"activities": ArrayUnion([new])})
    if "readtime" not in user_meta:
        cur_data = {"readtime": [new["readtime"]]}
        st.session_state["user_ref"].set(cur_data, merge=True)
    else:
        st.session_state["user_ref"].update({"readtime": ArrayUnion([new["readtime"]])})
    save_time = ori_rt - new["readtime"]
    if save_time <= 0:
        save_time = 0
    if "save_time" not in user_meta:
        cur_data = {"save_time": [save_time]}
        st.session_state["user_ref"].set(cur_data, merge=True)
    else:
        st.session_state["user_ref"].update({"save_time": ArrayUnion([save_time])})


def update_positives(title, id, category, ner_loc, ner_org, ner_per):
    new = {
        "title": hash_text(title),
        "id": id,
        "category": category,
        "ner_loc": ner_loc,
        "ner_org": ner_org,
        "ner_per": ner_per,
        "date": datetime.now(),
        "readtime": round(
            (datetime.now() - st.session_state["read_start"]).total_seconds()
        ),
    }
    user_meta = st.session_state["user_ref"].get()
    user_meta = user_meta.to_dict()
    if "positive" not in user_meta:
        cur_data = {"positive": [new]}
        st.session_state["user_ref"].set(cur_data, merge=True)
    else:
        st.session_state["user_ref"].update({"positive": ArrayUnion([new])})


def update_negatives(title, id, category, ner_loc, ner_org, ner_per):
    new = {
        "title": hash_text(title),
        "id": id,
        "category": category,
        "ner_loc": ner_loc,
        "ner_org": ner_org,
        "ner_per": ner_per,
        "date": datetime.now(),
        "readtime": round(
            (datetime.now() - st.session_state["read_start"]).total_seconds()
        ),
    }
    user_meta = st.session_state["user_ref"].get()
    user_meta = user_meta.to_dict()
    if "negative" not in user_meta:
        cur_data = {"negative": [new]}
        st.session_state["user_ref"].set(cur_data, merge=True)
    else:
        st.session_state["user_ref"].update({"negative": ArrayUnion([new])})


def generate_anno_text(text_list, label, color="#8ef", border="1px dashed red"):
    annos = []
    for txt in text_list:
        annos.append(
            annotation(
                txt.replace(" ##", "").replace("##", ""),
                label,
                color=color,
                border="1px dashed red",
            )
        )
    return annotated_text(*annos)


def second_to_text(duration=0, simplify=False):
    minute = duration // 60
    second = duration % 60
    result = ""
    if minute:
        result += f'{minute} {"min " if minute == 1 else "mins "}'
    if second:
        result += f'{second} {"sec" if second == 1 else "secs"}'

    if simplify:
        result = (
            result.replace("mins", "m")
            .replace("min", "m")
            .replace("secs", "s")
            .replace("sec", "s")
        )
    return result if not result == "" else "0 secs"


# TODO: Save article features
# TODO: Handle the multiple click of thumbs up and down
def summary_layout_template(
    title,
    id,
    author,
    publish,
    image,
    summary,
    sim,
    diff,
    reference,
    category,
    ori_tot_readtime,
    ner_loc,
    ner_org,
    ner_per,
):
    col1, col2, col3 = st.columns([0.2, 0.6, 0.2])
    similarity = sim[1:].split("- ")
    difference = diff[1:].split("- ")
    reading_time = readtime.of_text(summary + " ".join(similarity + difference)).seconds
    with col2:
        go_back_to_feed = st.button(
            "Back To Feed",
            on_click=update_activities,
            kwargs={
                "title": title,
                "id": id,
                "category": category,
                "summary_rt": reading_time,
                "ori_rt": ori_tot_readtime,
                "ner_loc": ner_loc,
                "ner_org": ner_org,
                "ner_per": ner_per,
            },
        )
        if go_back_to_feed:
            st.session_state["page_name"] = "feed"
            switch_page("home")

        with st.container():
            st.markdown(
                """
            <style>
                .title {
                    text-align: center;
                    font-size: 200%;
                    font-weight: bold;
                    color: white;
                    margin-bottom: 10px;
                    padding: 10px;
                    border-radius: 10px;
                }
                .author-publish {
                    text-align: center;
                    font-size: 80%;
                    color: grey;
                    margin: 5px 0;
                }
                .read-time {
                    text-align: center;
                    font-size: 90%;
                    color: white;
                    margin: 5px 0;
                }
                .summary-heading, .similarity-heading, .difference-heading {
                    text-align: justify;
                    font-size: 150%;
                    font-weight: bold;
                    color: white;
                    margin-top: 20px;
                    padding: 10px;
                    border-radius: 10px;
                    max-width: 90%;
                }
                .centered-image img {
                    display: block;
                    margin-left: auto;
                    margin-right: auto;
                    border-radius: 10px; /* Rounded corners */
                    max-width: 100%; /* Responsive */
                }
                .content {
                    text-align: justify;
                    margin: 0 auto;
                    max-width: 90%; /* Adjust to match the image width */
                }
            </style>
            """,
                unsafe_allow_html=True,
            )

            st.markdown("<div class='rounded-container'>", unsafe_allow_html=True)
            st.markdown(f"<div class='title'>{title}</div>", unsafe_allow_html=True)
            st.markdown(
                f"<div class='centered-image'><img src='{image}' alt='Image'></div>",
                unsafe_allow_html=True,
            )  # Centered and rounded image
            st.markdown(
                f"<p class='author-publish'>category: {category}</p>",
                unsafe_allow_html=True,
            )
            st.markdown(
                f"<p class='author-publish'>author: {author}</p>",
                unsafe_allow_html=True,
            )
            st.markdown(
                f"<p class='author-publish'>published: {publish}</p>",
                unsafe_allow_html=True,
            )
            st.markdown(
                f"<p class='author-publish'>reference article number: {len(reference)}</p>",
                unsafe_allow_html=True,
            )
            st.markdown(
                f"<p class='read-time'>read time: {second_to_text(reading_time)}</p>",
                unsafe_allow_html=True,
            )
            st.markdown(
                f"<p class='read-time'>NewsGPT help you save: {second_to_text(ori_tot_readtime - reading_time)}</p>",
                unsafe_allow_html=True,
            )
            st.markdown("<br>", unsafe_allow_html=True)
            with st.expander("Tags"):
                if ner_org:
                    generate_anno_text(ner_org, label="ORG")
                if ner_per:
                    generate_anno_text(ner_per, label="PER")
                if ner_loc:
                    generate_anno_text(ner_loc, label="LOC")

            thumbtext, thumbbt1, thumbbt2, _ = st.columns([0.4, 0.1, 0.1, 0.4])
            is_like = thumbbt1.button(
                "👍",
                on_click=update_positives,
                kwargs={
                    "title": title,
                    "id": id,
                    "category": category,
                    "ner_loc": ner_loc,
                    "ner_org": ner_org,
                    "ner_per": ner_per,
                },
                help="I like the news content, please recommend more",
            )

            not_like = thumbbt2.button(
                "👎",
                on_click=update_negatives,
                kwargs={
                    "title": title,
                    "id": id,
                    "category": category,
                    "ner_loc": ner_loc,
                    "ner_org": ner_org,
                    "ner_per": ner_per,
                },
                help="I don't like the news content, please don't feed to me",
            )
            if is_like:
                st.toast(
                    f"Thanks for liking the summary and article: {title}", icon="👍"
                )

            if not_like:
                st.toast(
                    f"We will make the recommendation better for you. Trust us!",
                    icon="👎",
                )

            st.markdown(
                f"<div class='similarity-heading'>Similarity:</div><div class='content'><ul><li>{'</li><li>'.join(similarity)}</li></ul></div>",
                unsafe_allow_html=True,
            )
            st.markdown(
                f"<div class='difference-heading'>Difference:</div><div class='content'><ul><li>{'</li><li>'.join(difference)}</li></ul></div>",
                unsafe_allow_html=True,
            )
            st.markdown(
                f"<div class='summary-heading'>Summary:</div><div class='content'>{summary}</div>",
                unsafe_allow_html=True,
            )
            st.divider()
            with st.expander("Reference Article Links"):
                for r_i, ref in enumerate(reference):
                    st.markdown(f'{r_i}. [{ref["title"]}]({ref["url"]})')
            st.markdown("</div>", unsafe_allow_html=True)


def run_chat(payload, query_embed, ori_article_id, compare_num=5):
    st.session_state.messages = [
        {"role": "assistant", "content": f"Ask me a question about {payload['title']}"}
    ]

    st.session_state.initial_prompt = [
        'Summarize the content details in the "5W1H" approach (Who, What, When, Where, Why, and How) in bullet points',
    ]

    st.session_state.reading_time = 0

    center_running()  # st.spinner("Start Summarize, please wait patient for 30 secs"):

    params = {"q": "", "l": compare_num, "t": 0.9}

    data = {
        "e": query_embed,
    }

    # Convert to JSON
    data = json.dumps(data)
    # Send the request
    start = time.time()
    response = requests.post(
        f"{os.environ['QDRANT_LAMBDA_ENTRYPOINT']}/api/v1/search",
        params=params,
        data=data,
    )  # , headers=headers)
    print("retrieve data from qdrant (in run chat): {}".format(time.time()-start))
    if response.status_code != 200:
        st.session_state["page_name"] = "feed"
        st.session_state["error"] = f"run_summary, qdrant search error: {response.text}"
        return
    recommendation = response.json()
    documents, reference, rt, ner_p, ner_l, ner_o = [], [], [], set(), set(), set()

    #TODO Add original Title and content

    for rec in recommendation["result"]["articles"]:
        if rec["payload"]["body"]:
            cur_doc = (
                f'title: {rec["payload"]["title"]}, body: {rec["payload"]["body"]}'
            )
            reference.append(
                {"title": rec["payload"]["title"], "url": rec["payload"]["url"]}
            )
            rt.append(readtime.of_text(rec["payload"]["body"]).seconds)
            ner_p.update(set(rec["payload"]["named_entities"].get("PER", [])))
            ner_l.update(set(rec["payload"]["named_entities"].get("LOC", [])))
            ner_o.update(set(rec["payload"]["named_entities"].get("ORG", [])))
            documents.append(Document(text=cur_doc))

    start = time.time()
    if "service_context" not in st.session_state:
        st.session_state["service_context"] = ServiceContext.from_defaults(
            llm=OpenAI(
                model="gpt-3.5-turbo",
                temperature=0.2,
                chunk_size=1024,
                chunk_overlap=200,
                system_prompt="As an expert current affairs commentator and analyst,\
                                                                          your task is to answer the questions from the user related to the news articles",
            )
        )
    if "summary_idx_response_synthesizer" not in st.session_state:
        st.session_state["summary_idx_response_synthesizer"] = get_response_synthesizer(
            response_mode="tree_summarize", use_async=True
        )
    
    doc_summary_index = DocumentSummaryIndex.from_documents(
        documents=documents,
        service_context=st.session_state["service_context"],
        response_synthesizer=st.session_state["summary_idx_response_synthesizer"],
    )
    retriever = DocumentSummaryIndexRetriever(doc_summary_index)
    # configure response synthesizer
    if "response_synthesizer" not in st.session_state:
        st.session_state["response_synthesizer"] = get_response_synthesizer()

    # assemble query engine
    st.session_state["chat_engine"] = RetrieverQueryEngine(
        retriever=retriever,
        response_synthesizer=st.session_state["response_synthesizer"],
    )
    print("Prepare summary index: {}".format(time.time()-start))

    # st.session_state["cur_news_index"] = VectorStoreIndex.from_documents(documents, service_context=st.session_state["service_context"])
    # st.session_state["chat_engine"] = st.session_state["cur_news_index"].as_chat_engine(chat_mode="condense_question", verbose=True)
    st.session_state["active_chat_result"] = {
        "title": payload["title"],
        "image": payload["top_image"],
        "author": ", ".join(payload["authors"]) if payload["authors"] else "unknown",
        "publish": payload["date"],
        "reference": reference,
        "id": ori_article_id,
        "category": payload["category"],
        "ori_tot_readtime": sum(rt),  # in seconds
        "ner_loc": list(ner_l),
        "ner_org": list(ner_o),
        "ner_per": list(ner_p),
    }
    st.session_state["page_name"] = "chat_mode"
    st.session_state["read_start"] = datetime.now()


"""
def run_summary(payload, query_embed, ori_article_id, compare_num=5):

    gpt_proc_art_ref = st.session_state["firestore_db"].collection("gpt_processed_articles").document(ori_article_id)
    gpt_proc_art_ref_meta = gpt_proc_art_ref.get()
    if gpt_proc_art_ref_meta.exists:
        st.session_state["active_summary_result"] = gpt_proc_art_ref_meta.to_dict()
    else:

        center_running()#st.spinner("Start Summarize, please wait patient for 30 secs"):
        params = {
            "q": "",
            "l": compare_num,
            "t": 0.9
        }

        data = {
            "e": query_embed,
        }

        # Convert to JSON
        data = json.dumps(data)
        response = requests.post(f"{os.environ['QDRANT_LAMBDA_ENTRYPOINT']}/api/v1/search", params=params, data=data)#, headers=headers)
        if response.status_code != 200:
            st.session_state["page_name"] = "feed"
            st.session_state["error"] = f"run_summary, qdrant search error: {response.text}"
            return
        
        recommendation = response.json()
        articles, reference, rt, ner_p, ner_l, ner_o = [], [], [], set(), set(), set()
        for rec in recommendation["result"]["articles"]:
            if rec["payload"]["body"]:
                articles.append({"id": rec["id"], "body": rec["payload"]["body"]})
                reference.append({"title": rec["payload"]["title"], "url": rec["payload"]["url"]})
                rt.append(readtime.of_text(rec["payload"]["body"]).seconds)
                ner_p.update(set(rec["payload"]["named_entities"].get("PER", [])))
                ner_l.update(set(rec["payload"]["named_entities"].get("LOC", [])))
                ner_o.update(set(rec["payload"]["named_entities"].get("ORG", [])))
            else:
                print("Body is empty")

        art_data = {
            "a": articles
        }
        headers = {
                'summary-api-key': os.environ.get("SUMMARY_SERVICE_API_KEY", "")
            }
        # Convert to JSON
        art_data = json.dumps(art_data)
        response = requests.post("http://0.0.0.0:5001/api/v1/summary", data=art_data, headers=headers)
        if response.status_code != 200:
            st.session_state["page_name"] = "feed"
            st.session_state["error"] = f"run_summary, openai summary error: {response.text}"
            return
        summary = response.json()
        summary = summary["result"]
        st.session_state["active_summary_result"] = {
            "title": payload["title"],
            "image": payload["top_image"],
            "author": ", ".join(payload["authors"]) if payload["authors"] else "unknown",
            "publish": payload["date"],
            "summary": summary["summary"],
            "sim": summary["similarity"],
            "diff": summary["difference"],
            "reference": reference,
            'id': ori_article_id,
            'category': payload["category"],
            "ori_tot_readtime": sum(rt), # in seconds
            "ner_loc": list(ner_l),
            "ner_org": list(ner_o),
            "ner_per": list(ner_p)
        }
        gpt_proc_art_ref.set(st.session_state["active_summary_result"])
    # switch_page("summary")
    st.session_state["page_name"] = "summary"
    st.session_state["read_start"] = datetime.now()
"""
