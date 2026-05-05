import functions_framework
import json
import logging
import os
from google.cloud.firestore import Client as FirestoreClient
from datetime import datetime, timedelta
from typing import Dict, Tuple, Optional
from google.oauth2 import service_account
from googleapiclient.discovery import build
from dateutil import parser as dateutil_parser
import pytz
from flask import Flask
from flask import request
import requests

app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment variables
GROUPME_BOT_ID = os.environ.get('GROUPME_BOT_ID')
GROUPME_API_TOKEN = os.environ.get('GROUPME_API_TOKEN')
GROUPME_GROUP_ID = os.environ.get('GROUPME_GROUP_ID')
GOOGLE_CALENDAR_ID = os.environ.get('GOOGLE_CALENDAR_ID', 'primary')
PROJECT_ID = os.environ.get('GCP_PROJECT_ID')
ADMIN_USER_IDS = set(os.environ.get('ADMIN_USER_IDS', '').split(',')) if os.environ.get('ADMIN_USER_IDS') else set()

# Firestore client
db = FirestoreClient()

# Global state for approved users
APPROVED_USERS_COLLECTION = 'calendar_approved_users'
CONFIG_COLLECTION = 'calendar_config'

class CalendarManager:
    """Manages Google Calendar operations with conflict detection."""
    
    def __init__(self):
        """Initialize Google Calendar API service."""
        self.service = self._get_calendar_service()
    
    @staticmethod
    def _get_calendar_service():
        """Build and return Google Calendar API service using google-api-python-client."""
        # For Cloud Functions, use Application Default Credentials
        # Ensure your Cloud Function service account has Calendar API permissions
        from google.auth import default
        credentials, _ = default(scopes=[
            'https://www.googleapis.com/auth/calendar'
        ])
        return build('calendar', 'v3', credentials=credentials)
    
    def check_time_conflict(self, calendar_id: str, start_time: datetime, 
                           end_time: datetime, 
                           exclude_event_id: Optional[str] = None) -> Tuple[bool, Optional[Dict]]:
        """
        Check if a time slot has conflicts with existing events.
        
        Returns:
            Tuple[bool, Optional[Dict]]: (has_conflict, conflicting_event_dict)
        """
        try:
            events_result = self.service.events().list(
                calendarId=calendar_id,
                timeMin=start_time.isoformat(),
                timeMax=end_time.isoformat(),
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            
            events = events_result.get('items', [])
            
            # Filter out the event being updated (if applicable)
            if exclude_event_id:
                events = [e for e in events if e['id'] != exclude_event_id]
            
            # Check for conflicts
            for event in events:
                event_start = dateutil_parser.isoparse(event['start'].get('dateTime'))
                event_end = dateutil_parser.isoparse(event['end'].get('dateTime'))
                
                # Check if there's overlap
                if start_time < event_end and end_time > event_start:
                    return True, event
            
            return False, None
        
        except Exception as e:
            logger.error(f"Error checking time conflict: {e}")
            return False, None
    
    def find_available_slots(self, calendar_id: str, start_time: datetime, 
                            duration_minutes: int, num_suggestions: int = 3) -> list:
        """
        Find available time slots starting from start_time.
        
        Returns:
            list: List of available time slots
        """
        available_slots = []
        current_time = start_time
        
        # Search up to 7 days ahead
        for _ in range(7 * 24):  # Hours
            test_end = current_time + timedelta(minutes=duration_minutes)
            has_conflict, _ = self.check_time_conflict(calendar_id, current_time, test_end)
            
            if not has_conflict:
                available_slots.append({
                    'start': current_time.strftime('%A, %B %d at %I:%M %p'),
                    'datetime': current_time
                })
            
            if len(available_slots) >= num_suggestions:
                break
            
            current_time += timedelta(hours=1)
        
        return available_slots
    
    def create_event(self, title: str, start_time: datetime, duration_minutes: int,
                    description: str = "", attendees: list = None) -> Tuple[bool, str, Optional[Dict]]:
        """
        Create a calendar event after checking for conflicts.
        
        Returns:
            Tuple[bool, str, Optional[Dict]]: (success, message, event_dict)
        """
        end_time = start_time + timedelta(minutes=duration_minutes)
        
        # Check for conflicts
        has_conflict, conflicting_event = self.check_time_conflict(
            GOOGLE_CALENDAR_ID, start_time, end_time
        )
        
        if has_conflict:
            conflict_summary = conflicting_event.get('summary', 'Untitled Event')
            conflict_start = conflicting_event['start'].get('dateTime', conflicting_event['start'].get('date'))
            conflict_end = conflicting_event['end'].get('dateTime', conflicting_event['end'].get('date'))
            
            conflict_msg = f"⚠️ **SCHEDULING CONFLICT:** '{conflict_summary}' is already scheduled from {conflict_start} to {conflict_end}\n\n"
            
            # Find available alternatives
            alternatives = self.find_available_slots(GOOGLE_CALENDAR_ID, start_time, duration_minutes, num_suggestions=3)
            if alternatives:
                conflict_msg += "💡 **Suggested Available Times:**\n"
                for slot in alternatives:
                    conflict_msg += f"  • {slot['start']}\n"
                conflict_msg += "\nWould you like to book at one of these times instead?"
            
            return False, conflict_msg, conflicting_event
        
        # Build event object
        event = {
            'summary': title,
            'description': description,
            'start': {
                'dateTime': start_time.isoformat(),
                'timeZone': 'America/New_York',
            },
            'end': {
                'dateTime': end_time.isoformat(),
                'timeZone': 'America/New_York',
            }
        }
        
        if attendees:
            event['attendees'] = attendees
        
        try:
            created_event = self.service.events().insert(
                calendarId=GOOGLE_CALENDAR_ID,
                body=event
            ).execute()
            
            message = f"✅ **Event Created:** '{title}'\n📅 {start_time.strftime('%A, %B %d at %I:%M %p')} ({duration_minutes} min)"
            return True, message, created_event
        
        except Exception as e:
            error_msg = f"❌ Error creating event: {str(e)}"
            logger.error(error_msg)
            return False, error_msg, None
    
    def delete_event(self, event_id: str) -> Tuple[bool, str]:
        """Delete a calendar event by ID."""
        try:
            self.service.events().delete(
                calendarId=GOOGLE_CALENDAR_ID,
                eventId=event_id
            ).execute()
            return True, f"✅ **Event Deleted:** Event ID {event_id}"
        except Exception as e:
            error_msg = f"❌ Error deleting event: {str(e)}"
            logger.error(error_msg)
            return False, error_msg
    
    def update_event(self, event_id: str, **kwargs) -> Tuple[bool, str, Optional[Dict]]:
        """
        Update a calendar event. Checks for conflicts if time is being changed.
        
        Kwargs can include: summary, description, start, end, etc.
        """
        try:
            # Get current event
            current_event = self.service.events().get(
                calendarId=GOOGLE_CALENDAR_ID,
                eventId=event_id
            ).execute()
            
            # Check for time conflicts if updating start/end times
            if 'start' in kwargs or 'end' in kwargs:
                new_start = kwargs.get('start', current_event['start'])
                new_end = kwargs.get('end', current_event['end'])
                
                start_dt = dateutil_parser.isoparse(new_start.get('dateTime', new_start.get('date')))
                end_dt = dateutil_parser.isoparse(new_end.get('dateTime', new_end.get('date')))
                
                has_conflict, conflicting_event = self.check_time_conflict(
                    GOOGLE_CALENDAR_ID, start_dt, end_dt, exclude_event_id=event_id
                )
                
                if has_conflict:
                    conflict_msg = f"⚠️ **SCHEDULING CONFLICT:** Cannot update to that time. '{conflicting_event.get('summary')}' is already scheduled."
                    return False, conflict_msg, None
            
            # Update the event
            updated_event = self.service.events().update(
                calendarId=GOOGLE_CALENDAR_ID,
                eventId=event_id,
                body={**current_event, **kwargs}
            ).execute()
            
            message = f"✅ **Event Updated:** {updated_event.get('summary')}"
            return True, message, updated_event
        
        except Exception as e:
            error_msg = f"❌ Error updating event: {str(e)}"
            logger.error(error_msg)
            return False, error_msg, None
    
    def list_upcoming_events(self, days: int = 7) -> list:
        """List upcoming events for the next N days."""
        try:
            now = datetime.now(pytz.timezone('America/New_York'))
            future = now + timedelta(days=days)
            
            events_result = self.service.events().list(
                calendarId=GOOGLE_CALENDAR_ID,
                timeMin=now.isoformat(),
                timeMax=future.isoformat(),
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            
            return events_result.get('items', [])
        
        except Exception as e:
            logger.error(f"Error listing events: {e}")
            return []


class UserAuthManager:
    """Manages approved user IDs and admin privileges."""
    
    @staticmethod
    def is_user_approved(user_id: str) -> bool:
        """Check if user ID is approved for calendar modifications."""
        try:
            user_doc = db.collection(APPROVED_USERS_COLLECTION).document(user_id).get()
            return user_doc.exists
        except Exception as e:
            logger.error(f"Error checking user approval: {e}")
            return False
    
    @staticmethod
    def is_admin(user_id: str) -> bool:
        """Check if user is an admin (can manage approved users list)."""
        return user_id in ADMIN_USER_IDS
    
    @staticmethod
    def add_approved_user(user_id: str, user_name: str = "") -> bool:
        """Add user to approved list."""
        try:
            db.collection(APPROVED_USERS_COLLECTION).document(user_id).set({
                'user_id': user_id,
                'user_name': user_name,
                'added_at': datetime.now(),
                'added_by_admin': True
            })
            return True
        except Exception as e:
            logger.error(f"Error adding approved user: {e}")
            return False
    
    @staticmethod
    def remove_approved_user(user_id: str) -> bool:
        """Remove user from approved list."""
        try:
            db.collection(APPROVED_USERS_COLLECTION).document(user_id).delete()
            return True
        except Exception as e:
            logger.error(f"Error removing approved user: {e}")
            return False
    
    @staticmethod
    def list_approved_users() -> list:
        """List all approved users."""
        try:
            docs = db.collection(APPROVED_USERS_COLLECTION).stream()
            return [doc.to_dict() for doc in docs]
        except Exception as e:
            logger.error(f"Error listing approved users: {e}")
            return []


class MessageParser:
    """Parse GroupMe messages and extract calendar intent."""
    
    CALENDAR_KEYWORDS = ['event', 'meeting', 'schedule', 'calendar', 'add', 'create', 'book', 'reserve']
    
    @staticmethod
    def extract_datetime(text: str) -> Optional[datetime]:
        """
        Extract datetime from natural language text.
        Supports: "tomorrow at 2pm", "next monday at 3:30pm", "2024-05-15 14:00", etc.
        """
        try:
            # Try to parse with dateutil parser
            parsed = dateutil_parser.parse(text, fuzzy=True)
            # Ensure it's timezone-aware
            if parsed.tzinfo is None:
                tz = pytz.timezone('America/New_York')
                parsed = tz.localize(parsed)
            return parsed
        except:
            return None
    
    @staticmethod
    def is_calendar_intent(message: str) -> bool:
        """Check if message is about calendar operations."""
        message_lower = message.lower()
        return any(keyword in message_lower for keyword in MessageParser.CALENDAR_KEYWORDS)


class GroupMeManager:
    """Handle GroupMe API interactions."""
    
    @staticmethod
    def send_message(message: str) -> bool:
        """Send a message to the GroupMe group."""
        if not GROUPME_BOT_ID:
            logger.warning("GROUPME_BOT_ID not configured")
            return False
            
        url = 'https://api.groupme.com/v3/bots/post'
        
        payload = {
            'bot_id': GROUPME_BOT_ID,
            'text': message
        }
        
        try:
            response = requests.post(url, json=payload)
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error sending GroupMe message: {message} :: {e}")
            return False


class CommandHandler:
    """Handle specific calendar commands from GroupMe."""
    
    def __init__(self):
        self.calendar_manager = CalendarManager()
        self.auth_manager = UserAuthManager()
    
    def handle_help(self) -> str:
        """Display help message for calendar commands."""
        #• `update event: [event ID] to [new title] on [new date/time]` - Update event
        help_text = """📅 **Calendar Bot Help**

**Calendar Commands:**
• `create event: Event Name on [date] at [time] for [min]` - Create an event
  Example: `create event: Team Meeting on Friday at 2pm for 90`
• `list events` or `show calendar` - Show upcoming 7 days of events
• `list events month` or `show calendar month` - Show upcoming 30 days of events
  Add `id` to list events to get id included in response, used to then delete or update event
• `delete event: [event ID]` - Delete an event

**Admin Commands** (Admins Only):
• `admin add: [user_id] [user_name]` - Approve user for calendar access
• `admin remove: [user_id]` - Revoke calendar access
• `admin list` - Show all approved users
• `admin all users` - Show all users in groupme

**Features:**
✅ Automatic conflict detection - prevents double-booking
✅ Suggests alternate times when conflicts found
✅ User approval system - only approved users can modify events
✅ Admin management - separate tier for access control
✅ Natural language dates: "tomorrow", "next friday", "May 15th at 3:30pm"
✅ Timezone: America/New_York

Type `help` for this message anytime!"""
        return help_text.strip()
    
    def handle_create_event(self, user_id: str, user_name: str, message: str) -> str:
        """Handle event creation request."""
        # Check if user is approved
        if not self.auth_manager.is_user_approved(user_id):
            return f"❌ **Permission Denied:** {user_name}, you're not approved to create events. Ask an admin!"
        
        try:
            # Parse event details from message
            # Format: "create event: Event Name on [date] at [time]"
            if 'on' not in message.lower():
                return "❌ Format: `create event: Event Name on [date] at [time]`\nExample: `create event: Movie night on Friday at 7pm`"
            
            # Extract event name and datetime
            create_idx = message.lower().find('create event:')
            if create_idx == -1:
                return "❌ Format: `create event: Event Name on [date] at [time]`"
            
            message_content = message[create_idx + 13:].strip()
            parts = message_content.split(' on ', 1)
            event_name = parts[0].strip()
            
            if not event_name:
                return "❌ Event name required. Format: `create event: Event Name on [date] at [time]`"
            
            # Extract date and time
            rest = parts[1] if len(parts) > 1 else ""
            duration_minutes = 60  # Default 1 hour
            
            # Check for duration
            if ' for ' in rest:
                rest, duration_part = rest.rsplit(' for ', 1)
                try:
                    duration_minutes = int(duration_part.split()[0])
                except:
                    pass
            
            # Parse datetime
            event_datetime = MessageParser.extract_datetime(rest)
            if not event_datetime:
                return f"❌ Could not parse date/time: '{rest}'\nTry: 'tomorrow at 2pm' or 'Friday at 3:30pm'"
            
            # Create the event
            success, response_msg, event = self.calendar_manager.create_event(
                title=event_name,
                start_time=event_datetime,
                duration_minutes=duration_minutes,
                description=f"Created by {user_name}"
            )
            
            return response_msg
        
        except Exception as e:
            logger.error(f"Error in handle_create_event: {e}")
            return f"❌ Error creating event: {str(e)}"

    def handle_delete_event(self, user_id: str, eventid: str) -> str:
        """Handle event creation request."""
        # Check if user is approved
        if not self.auth_manager.is_user_approved(user_id):
            return f"❌ **Permission Denied:** {user_name}, you're not approved to delete events. Ask an admin!"
        
        try:
            # delete the event
            success, response_msg = self.calendar_manager.delete_event(
                event_id=eventid
            )
            
            return response_msg
        
        except Exception as e:
            logger.error(f"Error in handle_delete_event: {e}")
            return f"❌ Error handling delete event: {str(e)}"
            
    
    def handle_list_events(self, showid, nbrDays) -> str:
        """Handle list events request."""
        try:
            events = self.calendar_manager.list_upcoming_events(days=nbrDays)
            
            if not events:
                return "📅 No upcoming events in the next " +nbrDays+ " days!"
            
            response = "📅 **Upcoming Events:**\n"
            for i, event in enumerate(events[:10], 1):
                print("EVENT: "+json.dumps(event))
                title = event.get('summary', 'Untitled')
                start = event['start'].get('dateTime', event['start'].get('date', 'TBD'))
                id = event.get('id',"")
                # response += f"{i}. {title} - {start}\n"
                # Format the datetime/date string
                if start != 'TBD':
                    try:
                        # Parse ISO format datetime or date
                        dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
                        # Format as "MMM DD" and "HH:MM am/pm"
                        formatted_start = dt.strftime('%b %d at %I:%M %p')
                    except ValueError:
                        # If it's a date-only string (YYYY-MM-DD)
                        dt = datetime.strptime(start, '%Y-%m-%d')
                        formatted_start = dt.strftime('%b %d')
                else:
                    formatted_start = 'TBD'
                if showid:
                    response += f"{i}. {title}: {id} - {formatted_start}\n"
                else:
                    response += f"{i}. {title} - {formatted_start}\n"
            
            return response
        
        except Exception as e:
            logger.error(f"Error listing events: {e}")
            return f"❌ Error listing events: {str(e)}"
    
    def handle_admin_add(self, admin_user_id: str, user_id: str, user_name: str) -> str:
        """Handle adding an approved user (admin only)."""
        if not self.auth_manager.is_admin(admin_user_id):
            return "❌ Only admins can add users."
        
        if self.auth_manager.add_approved_user(user_id, user_name):
            return f"✅ Added {user_name} ({user_id}) to approved users"
        return "❌ Failed to add user"
    
    def handle_admin_remove(self, admin_user_id: str, user_id: str) -> str:
        """Handle removing an approved user (admin only)."""
        if not self.auth_manager.is_admin(admin_user_id):
            return "❌ Only admins can remove users."
        
        if self.auth_manager.remove_approved_user(user_id):
            return f"✅ Removed user {user_id} from approved users"
        return "❌ Failed to remove user"
    
    def handle_admin_list(self) -> str:
        """Handle listing approved users."""
        users = self.auth_manager.list_approved_users()
        
        if not users:
            return "📋 No approved users yet"
        
        response = "👥 **Approved Users:**\n"
        for user in users:
            response += f"• {user.get('user_name', 'Unknown')} ({user.get('user_id', 'N/A')})\n"
        
        return response

    def handle_admin_allUsers(self) -> str:
        response = "👥 **All Users:**\n"
        """Send a message to the GroupMe group."""
        if not GROUPME_BOT_ID:
            logger.warning("GROUPME_BOT_ID not configured")
            return False
            
        #url = 'https://api.groupme.com/v3/groups/'+GROUPME_GROUP_ID
        url = 'https://api.groupme.com/v3/groups/'+GROUPME_GROUP_ID+"?token="+GROUPME_API_TOKEN
        
        payload = {
            'token': GROUPME_API_TOKEN
        }
        
        try:
            #response = requests.post(url, json=payload)
            response = requests.get(url)
            response.raise_for_status()
            respjson = response.json()
            response = "👥 **Available Users:**\n"
            #print("USERS-response: " +json.dumps(respjson['response']))
            #print("USERS-response-members: " +json.dumps(respjson['response']['members']))
            for member in respjson['response']['members']:
                response += f"• {member['name']} ({member['user_id']})\n"
            return response
        except Exception as e:
            logger.error(f"Error getting GroupMe users: {e}")
            return "Error getting GroupMe users"


@app.route("/", methods=['GET','POST'])
def calendar_agent(request):
    """
    Main HTTP Cloud Function that receives GroupMe webhook messages.
    
    Expected JSON payload from GroupMe:
    {
        "text": "create event: Team Meeting on Friday at 2pm",
        "user_id": "12345",
        "group_id": "67890",
        "name": "John"
    }
    """
    try:
        request_json = request.get_json(silent=True)
        
        if not request_json:
            return {"status": "error", "message": "No JSON provided"}, 400
        print("MSG: " + json.dumps(request_json))
        
        # Extract message details
        text = request_json.get("text", "").strip()
        user_id = request_json.get("user_id", "")
        group_id = request_json.get("group_id", "")
        user_name = request_json.get("name", "User")
        sender_id = request_json.get("sender_id", "")
        sender_type = request_json.get("sender_type", "")
        
        if not text or not user_id or not group_id:
            return {"status": "ignored", "message": "Missing required fields"}, 200
        
        # Don't process bot's own messages
        if (user_name == "CalendarMgr" and sender_type == "bot") or sender_id == "909774":
            return {"status": "ignored"}, 200
        
        # Initialize handler
        handler = CommandHandler()
        response_message = None
        
        text_lower = text.lower()
        
        # Help command
        if ('help' in text_lower or 'cal help' in text_lower) and 'Calendar Bot Help' not in text and 'Type `help`' not in text:
            response_message = handler.handle_help()
        
        # Create event
        elif 'create event' in text_lower:
            response_message = handler.handle_create_event(user_id, user_name, text)
        
        # Delete event
        elif 'delete event:' in text_lower:
            event_id = text.replace("delete event: ", "", 1).strip()
            response_message = handler.handle_delete_event(user_id, event_id)
            print(response_message)
            
        # List events
        elif 'list events' in text_lower or 'show calendar' in text_lower:
            showid = False
            if 'id' in text_lower:
                showid = True
            if 'month' in text_lower:
                response_message = handler.handle_list_events(showid, 30)
            else:
                response_message = handler.handle_list_events(showid, 7)
        
        # Admin commands
        elif 'admin add:' in text_lower:
            # Parse: admin add: user_id user_name
            parts = text.split('admin add:', 1)[1].strip().split(maxsplit=1)
            if len(parts) >= 1:
                target_user = parts[0]
                target_name = parts[1] if len(parts) > 1 else target_user
                response_message = handler.handle_admin_add(user_id, target_user, target_name)
        
        elif 'admin remove:' in text_lower:
            # Parse: admin remove: user_id
            parts = text.split('admin remove:', 1)[1].strip().split()
            if len(parts) >= 1:
                target_user = parts[0]
                response_message = handler.handle_admin_remove(user_id, target_user)
        
        elif 'admin list' in text_lower:
            response_message = handler.handle_admin_list()

        elif 'admin all users' in text_lower:
            response_message = handler.handle_admin_allUsers()
            
        # Only send response if we matched a command
        if response_message:
            GroupMeManager.send_message(response_message)
            return {"status": "success", "message_sent": True}, 200
        
        # Unknown command - ignore
        return {"status": "ignored"}, 200
    
    except Exception as e:
        logger.error(f"Error in calendar_agent: {e}")
        error_msg = f"❌ Error: {str(e)}"
        GroupMeManager.send_message(error_msg)
        return {"status": "error", "message": str(e)}, 500

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
