"""add device and provisioning models

Revision ID: 56cca7669edf
Revises: e1d5df2b124c
Create Date: 2026-05-03 09:35:26.682446

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '56cca7669edf'
down_revision: Union[str, None] = 'e1d5df2b124c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create device_provisioning_sessions table
    op.create_table('device_provisioning_sessions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('hardware_id', sa.String(length=128), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('challenge_nonce', sa.String(length=255), nullable=False),
    sa.Column('request_id', sa.UUID(), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('failure_code', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_device_provisioning_sessions_status'), 'device_provisioning_sessions', ['status'], unique=False)
    op.create_index(op.f('ix_device_provisioning_sessions_user_id'), 'device_provisioning_sessions', ['user_id'], unique=False)

    # 2. Create devices table
    op.create_table('devices',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('device_name', sa.String(length=100), nullable=False),
    sa.Column('hardware_id', sa.String(length=128), nullable=False),
    sa.Column('connectivity_status', sa.String(length=16), nullable=False, server_default='unknown'),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_rtt_ms', sa.Integer(), nullable=True),
    sa.Column('active_state', sa.String(length=16), nullable=False, server_default='standby'),
    sa.Column('binding_status', sa.String(length=16), nullable=False, server_default='bound'),
    sa.Column('livekit_room_name', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_devices_hardware_id'), 'devices', ['hardware_id'], unique=False)
    op.create_index(op.f('ix_devices_user_id'), 'devices', ['user_id'], unique=False)
    
    # 3. Add custom indices for device management
    op.create_index('uq_devices_hardware_id_ci', 'devices', [sa.text('lower(trim(hardware_id))')], unique=True)
    op.create_index('uq_devices_one_active_per_user', 'devices', ['user_id'], unique=True, postgresql_where=sa.text("active_state = 'active'"))


def downgrade() -> None:
    op.drop_index('uq_devices_one_active_per_user', table_name='devices', postgresql_where=sa.text("active_state = 'active'"))
    op.drop_index('uq_devices_hardware_id_ci', table_name='devices')
    op.drop_index(op.f('ix_devices_user_id'), table_name='devices')
    op.drop_index(op.f('ix_devices_hardware_id'), table_name='devices')
    op.drop_table('devices')
    op.drop_index(op.f('ix_device_provisioning_sessions_user_id'), table_name='device_provisioning_sessions')
    op.drop_index(op.f('ix_device_provisioning_sessions_status'), table_name='device_provisioning_sessions')
    op.drop_table('device_provisioning_sessions')
