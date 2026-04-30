# main.py
import functions_framework
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, Tuple, Optional
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from google.cloud import firestore
from dateutil import parser as dateutil_parser
import pytz

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment variables
GROUPME_BOT_ID = os.environ.get('GROUPME_BOT_ID')
GOOGLE_CALENDAR_ID = os.environ.get('GOOGLE_CALENDAR_ID', 'primary')
PROJECT_ID = os.environ.get('GCP_PROJECT_ID')
ADMIN_USER_IDS = set(os.environ.get('ADMIN_USER_IDS', '').split(','))

# Firestore client
db = firestore.client()

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
        """Build and return Google Calendar API service."""
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
            conflict_msg = f"**Conflict Found:** '{conflicting_event.get('summary')}' is scheduled from {conflicting_event['start'].get('dateTime', conflicting_event['start'].get('date'))} to {conflicting_event['end'].get('dateTime', conflicting_event['end'].get('date'))}"
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
            
            message = f"✅ **Event Created:** '{title}' on {start_time.strftime('%A, %B %d at %I:%M %p')}"
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
                    conflict_msg = f"**Conflict Found:** '{conflicting_event.get('summary')}' is already scheduled at that time."
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
            # Simple heuristic: try to parse with dateutil parser
            parsed = dateutil_parser.parse(text, fuzzy=True)
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
            logger.error(f"Error sending GroupMe message: {e}")
            return False


class CommandHandler:
    """Handle specific calendar commands from GroupMe."""
    
    def __init__(self):
        self.calendar_manager = CalendarManager()
        self.auth_manager = UserAuthManager()
    
    def handle_help(self) -> str:
        """Display help message for calendar commands."""
        help_text = """
📅 **Calendar Bot Help**

**Create Event:**
`@calendar_bot create: Event Name on [date] at [time] for [duration] minutes`
Example: `@calendar_bot create: Team Meeting on Friday at 2pm for 60 minutes`

**Delete Event:**
`@calendar_bot delete: [event_id]`

**List Events:**
`@calendar_bot list events` or `@calendar_bot show calendar`

**Update Event:**
`@calendar_bot update: [event_id] to [new title] on [new date/time]`

**Admin Commands (Admins Only):**
`@calendar_bot admin: add [user_id] as [user_name]`
`@calendar_bot admin: remove [user_id]`
`@calendar_bot admin: list approved users`

**Notes:**
- You must be approved to create/modify events. Ask an admin!
- Dates can be natural language: "tomorrow", "next monday", "May 15th at 3:30pm"
- The bot checks for scheduling conflicts before creating events
- Timezone: America/New_York
        """
        return help_text.strip()
    
    def handle_create_event(self, user_id: str, user_name: str, message: str) -> str:
        """Handle event creation request."""
        # Check if user is approved
        if not self.auth_manager.is_user_approved(user_id):
            return f"❌ **Permission Denied:** {user_name}, you are not approved to create events. Contact an admin for access."
        
        # Parse event details from message
        # Simple parser: "create: Event Name on [date] at [time] for [duration] minutes"
        try:
            # Extract event name
            if 'on' not in message.lower():
                return "❌ **Invalid Format:** Please specify a date. Format: 'create: Event Name on [date] at [time]'"
            
            parts = message.split(' on ', 1)
            event_name = parts[0].replace('create:', '').strip()
            
            if not event_name:
                return "❌ **Event Name Required:** Please provide an event name."
            
            # Extract date and time
            rest = parts[1]
            duration_minutes = 60  # Default
            
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
                return f"❌ **Invalid Date/Time:** Could not parse '{rest}'. Try: 'tomorrow at 2pm' or 'Friday at 3:30pm'"
            
            # Ensure datetime is timezone-aware
            if event_datetime.tzinfo is None:
                tz = pytz.timezone('America/New_York')
                event_datetime = tz.localize(event_datetime)
            
            # Create the event
            success, message_response, event = self.calendar_manager.create_event(
                title=event_name,
                start_time=event_datetime,
                duration_minutes=duration_minutes,
                description=f"Created by {user_name}"
            )
            
            if success:
                return message_response
            else:
                #
